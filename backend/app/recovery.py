from __future__ import annotations

import base64
import datetime as dt
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any, Iterable
from uuid import UUID

from sqlalchemy import MetaData, create_engine, inspect, select, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.engine.url import URL, make_url


MANIFEST_VERSION = 2
PROJECTION_TABLES = {
    "financial_projection_state",
    "financial_account_balance_projections",
}
SAFE_RESTORE_DATABASE_RE = re.compile(r"^[A-Za-z0-9_]+$")
SAFE_RESTORE_DATABASE_MARKERS = ("restore_test", "dr_test")


class RecoveryError(RuntimeError):
    pass


class RecoveryVerificationError(RecoveryError):
    pass


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, bytes):
        return {"__bytes_b64__": base64.b64encode(value).decode("ascii")}
    if isinstance(value, dict):
        return {str(k): _json_value(v) for k, v in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        return [_json_value(v) for v in value]
    return str(value)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        _json_value(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


_SCHEMA_WHITESPACE_RE = re.compile(r"\s+")
_LITERAL_VARCHAR_ARRAY_TO_TEXT_RE = re.compile(
    r"(ARRAY\[(?:'(?:''|[^'])*'::character varying(?:,\s*)?)+\])::text\[\]"
)


def _canonicalize_schema_definition(*, kind: str, definition: str) -> str:
    normalized = _SCHEMA_WHITESPACE_RE.sub(" ", definition.strip())

    if kind == "check_constraint":
        normalized = normalized.replace(
            "::character varying::text",
            "::character varying",
        )
        normalized = _LITERAL_VARCHAR_ARRAY_TO_TEXT_RE.sub(r"\1", normalized)

    return normalized


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_rows(connection: Connection, table) -> tuple[int, str]:
    pk_columns = list(table.primary_key.columns)
    statement = select(table)
    if pk_columns:
        statement = statement.order_by(*pk_columns)

    digest = hashlib.sha256()
    count = 0

    if pk_columns:
        result = connection.execution_options(stream_results=True).execute(statement)
        for row in result.mappings():
            digest.update(_canonical_json(dict(row)))
            digest.update(b"\n")
            count += 1
        return count, digest.hexdigest()

    # Alembic's version table has no declared PK. For the rare no-PK table,
    # sort row digests so the snapshot is stable without depending on planner order.
    row_hashes: list[str] = []
    result = connection.execution_options(stream_results=True).execute(statement)
    for row in result.mappings():
        row_hashes.append(hashlib.sha256(_canonical_json(dict(row))).hexdigest())
        count += 1
    for row_hash in sorted(row_hashes):
        digest.update(row_hash.encode("ascii"))
        digest.update(b"\n")
    return count, digest.hexdigest()


def _sequence_state(
    connection: Connection,
    *,
    exclude_tables: Iterable[str] = (),
) -> list[dict[str, Any]]:
    excluded = set(exclude_tables)
    definitions = connection.execute(
        text(
            """
            SELECT s.schemaname, s.sequencename, s.start_value, s.min_value,
                   s.max_value, s.increment_by, s.cycle, s.cache_size,
                   tbl.relname AS owned_by_table
            FROM pg_sequences s
            JOIN pg_namespace ns ON ns.nspname = s.schemaname
            JOIN pg_class seq ON seq.relnamespace = ns.oid
                             AND seq.relname = s.sequencename
                             AND seq.relkind = 'S'
            LEFT JOIN pg_depend dep ON dep.objid = seq.oid
                                   AND dep.deptype IN ('a', 'i')
            LEFT JOIN pg_class tbl ON tbl.oid = dep.refobjid
            WHERE s.schemaname = 'public'
            ORDER BY s.sequencename
            """
        )
    ).mappings().all()

    sequences: list[dict[str, Any]] = []
    for row in definitions:
        if row["owned_by_table"] in excluded:
            continue
        name = row["sequencename"]
        quoted = _quote_identifier(name)
        runtime = connection.exec_driver_sql(
            f"SELECT last_value, is_called FROM public.{quoted}"
        ).mappings().one()
        sequences.append(
            {
                "name": name,
                "owned_by_table": row["owned_by_table"],
                "start_value": row["start_value"],
                "min_value": row["min_value"],
                "max_value": row["max_value"],
                "increment_by": row["increment_by"],
                "cycle": row["cycle"],
                "cache_size": row["cache_size"],
                "last_value": runtime["last_value"],
                "is_called": runtime["is_called"],
            }
        )
    return sequences


def _schema_objects(connection: Connection) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []

    trigger_rows = connection.execute(
        text(
            """
            SELECT c.relname AS table_name,
                   tg.tgname AS object_name,
                   pg_get_triggerdef(tg.oid, true) AS definition
            FROM pg_trigger tg
            JOIN pg_class c ON c.oid = tg.tgrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public'
              AND NOT tg.tgisinternal
            ORDER BY c.relname, tg.tgname
            """
        )
    ).mappings()
    for row in trigger_rows:
        rows.append(
            {
                "kind": "trigger",
                "table": row["table_name"],
                "name": row["object_name"],
                "definition": _canonicalize_schema_definition(
                    kind="trigger",
                    definition=row["definition"],
                ),
            }
        )

    constraint_rows = connection.execute(
        text(
            """
            SELECT c.relname AS table_name,
                   con.conname AS object_name,
                   con.contype AS constraint_type,
                   pg_get_constraintdef(con.oid, true) AS definition
            FROM pg_constraint con
            JOIN pg_class c ON c.oid = con.conrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public'
            ORDER BY c.relname, con.conname
            """
        )
    ).mappings()
    for row in constraint_rows:
        definition_kind = (
            "check_constraint"
            if row["constraint_type"] == "c"
            else "constraint"
        )
        rows.append(
            {
                "kind": "constraint",
                "table": row["table_name"],
                "name": row["object_name"],
                "definition": _canonicalize_schema_definition(
                    kind=definition_kind,
                    definition=row["definition"],
                ),
            }
        )

    index_rows = connection.execute(
        text(
            """
            SELECT tablename AS table_name,
                   indexname AS object_name,
                   indexdef AS definition
            FROM pg_indexes
            WHERE schemaname = 'public'
            ORDER BY tablename, indexname
            """
        )
    ).mappings()
    for row in index_rows:
        rows.append(
            {
                "kind": "index",
                "table": row["table_name"],
                "name": row["object_name"],
                "definition": _canonicalize_schema_definition(
                    kind="index",
                    definition=row["definition"],
                ),
            }
        )

    return rows


def snapshot_connection(
    connection: Connection,
    *,
    exclude_tables: Iterable[str] = (),
) -> dict[str, Any]:
    if connection.dialect.name != "postgresql":
        raise RecoveryError("TASK 17 recovery snapshots require PostgreSQL")

    excluded = set(exclude_tables)
    metadata = MetaData()
    metadata.reflect(bind=connection, schema="public")

    tables: dict[str, dict[str, Any]] = {}
    for key in sorted(metadata.tables):
        table = metadata.tables[key]
        name = table.name
        if name in excluded:
            continue
        rows, table_sha256 = _hash_rows(connection, table)
        tables[name] = {"rows": rows, "sha256": table_sha256}

    schema_objects = [
        row for row in _schema_objects(connection) if row.get("table") not in excluded
    ]
    schema_objects_sha256 = hashlib.sha256(_canonical_json(schema_objects)).hexdigest()
    sequences = _sequence_state(connection, exclude_tables=excluded)
    sequences_sha256 = hashlib.sha256(_canonical_json(sequences)).hexdigest()

    alembic_revision = None
    if "alembic_version" in tables:
        result = connection.execute(text("SELECT version_num FROM alembic_version LIMIT 1")).scalar()
        alembic_revision = result

    fingerprint_payload = {
        "alembic_revision": alembic_revision,
        "tables": tables,
        "schema_objects_sha256": schema_objects_sha256,
        "sequences_sha256": sequences_sha256,
    }
    database_sha256 = hashlib.sha256(_canonical_json(fingerprint_payload)).hexdigest()

    return {
        **fingerprint_payload,
        "database_sha256": database_sha256,
    }


def snapshot_engine(
    engine: Engine,
    *,
    exclude_tables: Iterable[str] = (),
) -> dict[str, Any]:
    with engine.connect().execution_options(isolation_level="REPEATABLE READ") as connection:
        connection.execute(text("SET TRANSACTION READ ONLY"))
        return snapshot_connection(connection, exclude_tables=exclude_tables)


def _libpq_environment(database_url: str) -> tuple[dict[str, str], URL]:
    url = make_url(database_url)
    if not url.drivername.startswith("postgresql"):
        raise RecoveryError("Backup/restore requires a PostgreSQL DATABASE_URL")
    if not url.database:
        raise RecoveryError("DATABASE_URL must include a database name")

    env = os.environ.copy()
    if url.host:
        env["PGHOST"] = url.host
    if url.port:
        env["PGPORT"] = str(url.port)
    if url.username:
        env["PGUSER"] = url.username
    if url.password:
        env["PGPASSWORD"] = url.password
    env["PGDATABASE"] = url.database

    sslmode = url.query.get("sslmode")
    if sslmode:
        env["PGSSLMODE"] = str(sslmode)
    return env, url


def safe_database_name(database_url: str) -> str:
    url = make_url(database_url)
    if not url.database:
        raise RecoveryError("DATABASE_URL must include a database name")
    return url.database


def validate_restore_target(*, source_database: str, target_database_url: str) -> str:
    target = safe_database_name(target_database_url)
    if target == source_database:
        raise RecoveryError("Refusing to restore over the source database")
    if not SAFE_RESTORE_DATABASE_RE.fullmatch(target):
        raise RecoveryError("Restore drill target database name may contain only letters, digits, and underscores")
    if not any(marker in target.lower() for marker in SAFE_RESTORE_DATABASE_MARKERS):
        raise RecoveryError(
            "Restore drill target database name must contain 'restore_test' or 'dr_test'"
        )
    return target


def _run_checked(argv: list[str], *, env: dict[str, str]) -> None:
    try:
        subprocess.run(argv, env=env, check=True)
    except FileNotFoundError as exc:
        raise RecoveryError(f"Required PostgreSQL CLI tool not found: {argv[0]}") from exc
    except subprocess.CalledProcessError as exc:
        raise RecoveryError(f"Command failed with exit code {exc.returncode}: {argv[0]}") from exc


def create_backup(*, database_url: str, output_dir: Path) -> tuple[Path, Path, dict[str, Any]]:
    pg_dump = shutil.which("pg_dump")
    if not pg_dump:
        raise RecoveryError("pg_dump is required for TASK 17 backups")

    env, url = _libpq_environment(database_url)
    engine = create_engine(database_url)
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
    database_name = url.database or "database"
    base_name = f"{database_name}-{timestamp}"
    dump_path = output_dir / f"{base_name}.dump"
    manifest_path = output_dir / f"{base_name}.manifest.json"
    partial_dump = dump_path.with_suffix(".dump.partial")
    partial_manifest = manifest_path.with_suffix(".json.partial")

    try:
        with engine.connect().execution_options(isolation_level="REPEATABLE READ") as connection:
            connection.execute(text("SET TRANSACTION READ ONLY"))
            exported_snapshot = connection.execute(text("SELECT pg_export_snapshot()")) .scalar_one()
            full_snapshot = snapshot_connection(connection)
            truth_snapshot = snapshot_connection(connection, exclude_tables=PROJECTION_TABLES)

            _run_checked(
                [
                    pg_dump,
                    "--format=custom",
                    "--no-owner",
                    "--no-privileges",
                    "--snapshot",
                    str(exported_snapshot),
                    "--file",
                    str(partial_dump),
                ],
                env=env,
            )

        dump_sha256 = sha256_file(partial_dump)
        manifest = {
            "manifest_version": MANIFEST_VERSION,
            "created_at_utc": utc_now().isoformat(),
            "source_database": database_name,
            "alembic_revision": full_snapshot["alembic_revision"],
            "dump_sha256": dump_sha256,
            "database_sha256": full_snapshot["database_sha256"],
            "truth_without_projections_sha256": truth_snapshot["database_sha256"],
            "tables": full_snapshot["tables"],
            "schema_objects_sha256": full_snapshot["schema_objects_sha256"],
            "sequences_sha256": full_snapshot["sequences_sha256"],
        }
        partial_manifest.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

        os.replace(partial_dump, dump_path)
        os.replace(partial_manifest, manifest_path)
        return dump_path, manifest_path, manifest
    finally:
        engine.dispose()
        for partial in (partial_dump, partial_manifest):
            if partial.exists():
                partial.unlink()


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RecoveryVerificationError(f"Cannot read backup manifest: {path}") from exc
    if manifest.get("manifest_version") != MANIFEST_VERSION:
        raise RecoveryVerificationError("Unsupported recovery manifest version")
    return manifest


def verify_dump_against_manifest(*, dump_path: Path, manifest: dict[str, Any]) -> None:
    actual = sha256_file(dump_path)
    expected = manifest.get("dump_sha256")
    if actual != expected:
        raise RecoveryVerificationError(
            f"Backup dump checksum mismatch: expected {expected}, got {actual}"
        )


def _admin_url(target_database_url: str) -> URL:
    target = make_url(target_database_url)
    return target.set(database="postgres")


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def recreate_restore_target(*, target_database_url: str) -> None:
    target_name = safe_database_name(target_database_url)
    admin_engine = create_engine(_admin_url(target_database_url), isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.connect() as connection:
            connection.execute(
                text(
                    """
                    SELECT pg_terminate_backend(pid)
                    FROM pg_stat_activity
                    WHERE datname = :database_name
                      AND pid <> pg_backend_pid()
                    """
                ),
                {"database_name": target_name},
            )
            quoted = _quote_identifier(target_name)
            connection.exec_driver_sql(f"DROP DATABASE IF EXISTS {quoted}")
            connection.exec_driver_sql(f"CREATE DATABASE {quoted}")
    finally:
        admin_engine.dispose()


def restore_backup(
    *,
    dump_path: Path,
    manifest_path: Path,
    target_database_url: str,
) -> dict[str, Any]:
    pg_restore = shutil.which("pg_restore")
    if not pg_restore:
        raise RecoveryError("pg_restore is required for TASK 17 restores")

    manifest = load_manifest(manifest_path)
    verify_dump_against_manifest(dump_path=dump_path, manifest=manifest)
    target_name = validate_restore_target(
        source_database=manifest["source_database"],
        target_database_url=target_database_url,
    )

    recreate_restore_target(target_database_url=target_database_url)
    env, _ = _libpq_environment(target_database_url)
    _run_checked(
        [
            pg_restore,
            "--exit-on-error",
            "--no-owner",
            "--no-privileges",
            "--dbname",
            target_name,
            str(dump_path),
        ],
        env=env,
    )

    engine = create_engine(target_database_url)
    try:
        restored = snapshot_engine(engine)
    finally:
        engine.dispose()

    if restored["database_sha256"] != manifest["database_sha256"]:
        raise RecoveryVerificationError(
            "Restored database fingerprint does not match the backup manifest"
        )
    if restored["schema_objects_sha256"] != manifest["schema_objects_sha256"]:
        raise RecoveryVerificationError(
            "Restored schema object fingerprint does not match the backup manifest"
        )
    if restored["sequences_sha256"] != manifest["sequences_sha256"]:
        raise RecoveryVerificationError(
            "Restored sequence state does not match the backup manifest"
        )
    return restored


def clear_projection_rows(*, database_url: str) -> None:
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM financial_account_balance_projections"))
            connection.execute(text("DELETE FROM financial_projection_state"))
    finally:
        engine.dispose()


def verify_truth_without_projections(*, database_url: str, expected_sha256: str) -> dict[str, Any]:
    engine = create_engine(database_url)
    try:
        snapshot = snapshot_engine(engine, exclude_tables=PROJECTION_TABLES)
    finally:
        engine.dispose()
    if snapshot["database_sha256"] != expected_sha256:
        raise RecoveryVerificationError(
            "Non-projection truth changed during recovery/rebuild verification"
        )
    return snapshot
