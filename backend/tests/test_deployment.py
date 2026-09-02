from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.database import engine
from app.deployment import (
    DeploymentConfigurationError,
    assert_startup_ready,
    database_revision_status,
    deployment_preflight,
    deployment_readiness,
    deployment_state,
    expected_database_heads,
    release_metadata,
)
from app.main import app
from scripts.deployment_smoke import fetch_json


pytestmark = pytest.mark.deployment
client = TestClient(app)


@pytest.fixture(autouse=True)
def reset_deployment_state():
    deployment_state.reset_for_tests()
    yield
    deployment_state.reset_for_tests()


def test_database_revision_matches_code_head():
    status = database_revision_status()
    assert status.current_is_head is True
    assert status.current == expected_database_heads()
    assert len(status.current) == 1


def test_readiness_includes_database_revision_and_release_metadata(monkeypatch):
    monkeypatch.setenv("APP_RELEASE", "v1-test")
    monkeypatch.setenv("GIT_SHA", "abc123")

    response = client.get("/health/ready")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ready"
    assert payload["schema"] == "ok"
    assert payload["current_revision"] == payload["expected_revision"]
    assert payload["release"] == "v1-test"
    assert payload["git_sha"] == "abc123"


def test_draining_process_is_not_ready_but_remains_live():
    deployment_state.mark_draining()

    ready = client.get("/health/ready")
    live = client.get("/health/live")

    assert ready.status_code == 503
    assert ready.json()["detail"]["deployment"] == "draining"
    assert live.status_code == 200
    assert live.json()["status"] == "ok"


def test_schema_revision_mismatch_fails_closed_without_persisting_corruption():
    with engine.connect() as connection:
        current = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()

    try:
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE alembic_version SET version_num = 'stale-test-revision'")
            )

        result = deployment_readiness()
        assert result["ready"] is False
        assert result["schema"] == "revision_mismatch"
        assert result["current_revision"] == ["stale-test-revision"]
        assert result["expected_revision"] != ["stale-test-revision"]

        with pytest.raises(DeploymentConfigurationError):
            assert_startup_ready()
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE alembic_version SET version_num = :revision"),
                {"revision": current},
            )

    assert database_revision_status().current_is_head is True


def test_preflight_checks_canonical_truth_and_is_read_only():
    before = database_revision_status()

    result = deployment_preflight(run_canonical_audit=True)

    after = database_revision_status()
    assert result["ready"] is True
    assert result["canonical_audit"] == "ok"
    assert result["canonical_failed_user_ids"] == []
    assert before == after


def test_preflight_can_skip_expensive_audit_for_diagnostics():
    result = deployment_preflight(run_canonical_audit=False)

    assert result["ready"] is True
    assert result["canonical_audit"] == "not_run"


def test_release_metadata_is_bounded_and_does_not_dump_environment(monkeypatch):
    monkeypatch.setenv("APP_RELEASE", "r" * 300)
    monkeypatch.setenv("GIT_SHA", "g" * 300)
    monkeypatch.setenv("SECRET_KEY", "should-never-appear")

    metadata = release_metadata()

    assert set(metadata) == {"release", "git_sha"}
    assert len(metadata["release"]) == 128
    assert len(metadata["git_sha"]) == 128
    assert "should-never-appear" not in json.dumps(metadata)


def test_smoke_fetch_helper_accepts_health_json(tmp_path, monkeypatch):
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"status":"ready"}'

    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda request, timeout: Response(),
    )

    status, payload = fetch_json("http://example.test/health/ready", 1.0)
    assert status == 200
    assert payload == {"status": "ready"}


def test_deployment_runbook_explicitly_separates_migration_and_startup():
    runbook = (Path(__file__).resolve().parents[1] / "DEPLOYMENT_V1.md").read_text()

    assert "No implicit migration on API startup" in runbook
    assert "alembic upgrade head" in runbook
    assert "never blindly downgrades financial schema" in runbook
    assert "forward fix" in runbook
