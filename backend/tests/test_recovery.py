from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.recovery import (
    PROJECTION_TABLES,
    RecoveryError,
    RecoveryVerificationError,
    _canonical_json,
    _canonicalize_schema_definition,
    _libpq_environment,
    load_manifest,
    validate_restore_target,
    verify_dump_against_manifest,
)

pytestmark = pytest.mark.disaster_recovery


def test_canonical_json_is_order_stable_and_decimal_exact():
    left = {"b": 2, "a": {"money": "100.00", "n": 1}}
    right = {"a": {"n": 1, "money": "100.00"}, "b": 2}
    assert _canonical_json(left) == _canonical_json(right)


def test_schema_fingerprint_normalizes_equivalent_postgres_check_casts():
    source = (
        "CHECK (account_type::text = ANY "
        "(ARRAY['BANK'::character varying, 'EWALLET'::character varying, "
        "'CREDIT_CARD'::character varying, 'CASH'::character varying]::text[]))"
    )
    restored = (
        "CHECK (account_type::text = ANY "
        "(ARRAY['BANK'::character varying::text, "
        "'EWALLET'::character varying::text, "
        "'CREDIT_CARD'::character varying::text, "
        "'CASH'::character varying::text]))"
    )

    assert _canonicalize_schema_definition(
        kind="check_constraint",
        definition=source,
    ) == _canonicalize_schema_definition(
        kind="check_constraint",
        definition=restored,
    )


def test_schema_fingerprint_does_not_hide_real_check_constraint_changes():
    expected = (
        "CHECK (account_type::text = ANY "
        "(ARRAY['BANK'::character varying, 'CASH'::character varying]::text[]))"
    )
    changed = (
        "CHECK (account_type::text = ANY "
        "(ARRAY['BANK'::character varying::text, "
        "'CREDIT_CARD'::character varying::text]))"
    )

    assert _canonicalize_schema_definition(
        kind="check_constraint",
        definition=expected,
    ) != _canonicalize_schema_definition(
        kind="check_constraint",
        definition=changed,
    )


def test_libpq_environment_keeps_password_out_of_database_name_and_supports_sslmode():
    env, url = _libpq_environment(
        "postgresql://alice:super-secret@db.example:5433/finance_test_db?sslmode=require"
    )
    assert env["PGPASSWORD"] == "super-secret"
    assert env["PGHOST"] == "db.example"
    assert env["PGPORT"] == "5433"
    assert env["PGUSER"] == "alice"
    assert env["PGDATABASE"] == "finance_test_db"
    assert env["PGSSLMODE"] == "require"
    assert url.database == "finance_test_db"


def test_restore_target_cannot_equal_source_database():
    with pytest.raises(RecoveryError, match="source database"):
        validate_restore_target(
            source_database="finance_test_db",
            target_database_url="postgresql://u:p@localhost/finance_test_db",
        )


def test_restore_target_requires_explicit_disposable_name():
    with pytest.raises(RecoveryError, match="restore_test.*dr_test"):
        validate_restore_target(
            source_database="finance_test_db",
            target_database_url="postgresql://u:p@localhost/finance_backup",
        )


def test_restore_target_accepts_dr_test_name():
    assert (
        validate_restore_target(
            source_database="finance_test_db",
            target_database_url="postgresql://u:p@localhost/finance_dr_test",
        )
        == "finance_dr_test"
    )


def test_dump_checksum_tampering_is_detected(tmp_path: Path):
    dump = tmp_path / "backup.dump"
    dump.write_bytes(b"original")
    manifest = {"dump_sha256": hashlib.sha256(b"original").hexdigest()}
    verify_dump_against_manifest(dump_path=dump, manifest=manifest)

    dump.write_bytes(b"tampered")
    with pytest.raises(RecoveryVerificationError, match="checksum mismatch"):
        verify_dump_against_manifest(dump_path=dump, manifest=manifest)


def test_manifest_rejects_unknown_version(tmp_path: Path):
    manifest = tmp_path / "backup.manifest.json"
    manifest.write_text(json.dumps({"manifest_version": 999}))
    with pytest.raises(RecoveryVerificationError, match="Unsupported"):
        load_manifest(manifest)


def test_projection_tables_are_explicitly_disposable():
    assert PROJECTION_TABLES == {
        "financial_projection_state",
        "financial_account_balance_projections",
    }
