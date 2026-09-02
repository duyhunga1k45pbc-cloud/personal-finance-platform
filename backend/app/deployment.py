from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.canonical_audit import audit_all_users
from app.database import SessionLocal, engine
from app.observability import check_database_readiness, log_event


class DeploymentConfigurationError(RuntimeError):
    """Raised when a release must not start serving traffic."""


@dataclass(frozen=True)
class RevisionStatus:
    current: tuple[str, ...]
    expected: tuple[str, ...]

    @property
    def current_is_head(self) -> bool:
        return self.current == self.expected


class DeploymentState:
    """Process-local serving lifecycle; never part of financial truth."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = "SERVING"

    def mark_serving(self) -> None:
        with self._lock:
            self._state = "SERVING"

    def mark_draining(self) -> None:
        with self._lock:
            self._state = "DRAINING"

    def snapshot(self) -> str:
        with self._lock:
            return self._state

    def reset_for_tests(self) -> None:
        self.mark_serving()


deployment_state = DeploymentState()


def _alembic_config() -> Config:
    backend_root = Path(__file__).resolve().parents[1]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "alembic"))
    return config


def expected_database_heads() -> tuple[str, ...]:
    script = ScriptDirectory.from_config(_alembic_config())
    return tuple(sorted(script.get_heads()))


def current_database_heads(db_engine: Engine = engine) -> tuple[str, ...]:
    try:
        with db_engine.connect() as connection:
            rows = connection.execute(text("SELECT version_num FROM alembic_version"))
            return tuple(sorted(str(row[0]) for row in rows))
    except Exception:
        return tuple()


def database_revision_status(db_engine: Engine = engine) -> RevisionStatus:
    return RevisionStatus(
        current=current_database_heads(db_engine),
        expected=expected_database_heads(),
    )


def release_metadata() -> dict[str, str]:
    """Low-risk release identifiers only; no environment dump or secrets."""

    def clean(name: str, default: str) -> str:
        value = os.getenv(name, default).strip()
        return value[:128] if value else default

    return {
        "release": clean("APP_RELEASE", "development"),
        "git_sha": clean("GIT_SHA", "unknown"),
    }


def deployment_readiness(
    *,
    database_readiness: dict[str, Any] | None = None,
) -> dict[str, Any]:
    state = deployment_state.snapshot()
    if state != "SERVING":
        return {
            "ready": False,
            "deployment": state.lower(),
            **release_metadata(),
        }

    database = (
        database_readiness
        if database_readiness is not None
        else check_database_readiness()
    )
    if not database["ready"]:
        return {
            **database,
            "deployment": "serving",
            **release_metadata(),
        }

    revision = database_revision_status()
    if not revision.current_is_head:
        return {
            "ready": False,
            "database": "ok",
            "schema": "revision_mismatch",
            "deployment": "serving",
            "current_revision": list(revision.current),
            "expected_revision": list(revision.expected),
            **release_metadata(),
        }

    return {
        "ready": True,
        "database": "ok",
        "schema": "ok",
        "deployment": "serving",
        "current_revision": list(revision.current),
        "expected_revision": list(revision.expected),
        **release_metadata(),
    }


def assert_startup_ready() -> dict[str, Any]:
    result = deployment_readiness()
    if not result["ready"]:
        raise DeploymentConfigurationError(
            "Deployment startup gate failed: database/schema is not ready"
        )
    return result


def deployment_preflight(*, run_canonical_audit: bool = True) -> dict[str, Any]:
    """Fail-closed deployment gate; intentionally does not run migrations."""

    readiness = deployment_readiness()
    result: dict[str, Any] = {
        "ready": bool(readiness["ready"]),
        "readiness": readiness,
        "canonical_audit": "not_run",
    }
    if not readiness["ready"]:
        return result

    if not run_canonical_audit:
        return result

    db = SessionLocal()
    try:
        audit = audit_all_users(db)
    finally:
        db.close()

    failed_user_ids = [row["user_id"] for row in audit if not row["ok"]]
    result["canonical_audit"] = "ok" if not failed_user_ids else "failed"
    result["canonical_users_checked"] = len(audit)
    result["canonical_failed_user_ids"] = failed_user_ids
    result["ready"] = not failed_user_ids
    return result


def mark_process_serving() -> None:
    deployment_state.mark_serving()
    log_event("deployment.serving", **release_metadata())


def mark_process_draining() -> None:
    deployment_state.mark_draining()
    log_event("deployment.draining", **release_metadata())
