from __future__ import annotations

import asyncio
import json
import logging
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.responses import Response

from app.database import Base, SessionLocal, engine
from app.main import app, observe_request
from app.models import FinancialAccount
from app.observability import (
    JsonFormatter,
    bind_request_id,
    current_request_id,
    durable_operational_snapshot,
)
from app.observability import runtime_metrics
from app.projection_service import (
    ProjectionStaleError,
    rebuild_user_projection,
    require_fresh_projection,
)
from app.provider_sync_service import sync_provider_connection
from app.reconciliation_service import detect_cash_reconciliation

pytestmark = pytest.mark.observability


def _clear_app_data():
    if engine.dialect.name == "postgresql":
        table_names = [
            engine.dialect.identifier_preparer.quote(table.name)
            for table in Base.metadata.sorted_tables
        ]
        if table_names:
            with engine.begin() as connection:
                connection.exec_driver_sql(
                    "TRUNCATE TABLE "
                    + ", ".join(table_names)
                    + " RESTART IDENTITY CASCADE"
                )
        return

    with engine.begin() as connection:
        for table in reversed(Base.metadata.sorted_tables):
            connection.execute(table.delete())


@pytest.fixture(autouse=True)
def reset_state():
    Base.metadata.create_all(bind=engine)
    _clear_app_data()
    runtime_metrics.reset_for_tests()
    yield
    runtime_metrics.reset_for_tests()
    _clear_app_data()


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _request_with_id(request_id: str) -> Request:
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/runtime-test",
            "raw_path": b"/runtime-test",
            "query_string": b"",
            "headers": [(b"x-request-id", request_id.encode("ascii"))],
            "client": ("testclient", 12345),
            "server": ("testserver", 80),
        }
    )


def _register_and_login(client: TestClient, prefix: str = "obs"):
    email = f"{prefix}-{uuid4().hex}@example.com"
    password = "testpassword123"
    registered = client.post(
        "/auth/register",
        json={"email": email, "password": password},
    )
    assert registered.status_code == 200, registered.text
    logged_in = client.post(
        "/auth/login",
        json={"email": email, "password": password},
    )
    assert logged_in.status_code == 200, logged_in.text
    return registered.json()["id"], {
        "Authorization": f"Bearer {logged_in.json()['access_token']}"
    }


def test_request_id_is_generated_echoed_and_measured(client):
    response = client.get("/")
    assert response.status_code == 200
    request_id = response.headers["x-request-id"]
    assert request_id
    assert len(request_id) <= 128

    snapshot = runtime_metrics.snapshot()
    assert snapshot["counters"]["http_requests_total"] == 1
    row = next(
        item
        for item in snapshot["http"]
        if item["method"] == "GET" and item["route"] == "/"
    )
    assert row["status_code"] == 200
    assert row["count"] == 1
    assert row["duration_ms_max"] >= 0


def test_safe_request_id_is_preserved_and_invalid_value_is_replaced(client):
    accepted = client.get("/", headers={"X-Request-ID": "job-123.trace:abc"})
    assert accepted.headers["x-request-id"] == "job-123.trace:abc"

    rejected = client.get("/", headers={"X-Request-ID": "bad request id"})
    assert rejected.headers["x-request-id"] != "bad request id"
    assert rejected.headers["x-request-id"]


@pytest.mark.anyio
async def test_observe_request_cancellation_resets_context_in_same_task():
    first_request_id = "cancelled-runtime-request"
    second_request_id = "after-cancel-runtime-request"
    downstream_entered = asyncio.Event()
    never_release = asyncio.Event()
    observed_contexts = []

    async def downstream(request):
        request_id = request.headers["x-request-id"]
        observed_contexts.append((request_id, current_request_id()))
        if request_id == first_request_id:
            downstream_entered.set()
            await never_release.wait()
        return Response(content="ok")

    async def cancel_then_run_followup():
        cancellation_propagated = False
        context_after_cancellation = "not-checked"
        try:
            await observe_request(
                _request_with_id(first_request_id),
                downstream,
            )
        except asyncio.CancelledError:
            cancellation_propagated = True
            context_after_cancellation = current_request_id()

        response = await observe_request(
            _request_with_id(second_request_id),
            downstream,
        )
        return (
            cancellation_propagated,
            context_after_cancellation,
            current_request_id(),
            response,
        )

    task = asyncio.create_task(cancel_then_run_followup())
    try:
        await asyncio.wait_for(downstream_entered.wait(), timeout=3)
        task.cancel()
        (
            cancellation_propagated,
            context_after_cancellation,
            context_after_followup,
            response,
        ) = await asyncio.wait_for(task, timeout=3)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert cancellation_propagated is True
    assert context_after_cancellation is None
    assert context_after_followup is None
    assert observed_contexts == [
        (first_request_id, first_request_id),
        (second_request_id, second_request_id),
    ]
    assert response.headers["X-Request-ID"] == second_request_id


@pytest.mark.anyio
async def test_overlapping_observe_requests_keep_contexts_and_metrics_isolated():
    request_ids = ("overlap-runtime-request-a", "overlap-runtime-request-b")
    both_downstream_entered = asyncio.Event()
    release_downstreams = asyncio.Event()
    observed_contexts = {}
    metrics_before = runtime_metrics.snapshot()["counters"].get(
        "http_requests_total",
        0,
    )

    async def downstream(request):
        request_id = request.headers["x-request-id"]
        observed_contexts[request_id] = current_request_id()
        if len(observed_contexts) == len(request_ids):
            both_downstream_entered.set()
        await release_downstreams.wait()
        return Response(content="ok")

    tasks = [
        asyncio.create_task(
            observe_request(_request_with_id(request_id), downstream)
        )
        for request_id in request_ids
    ]
    try:
        await asyncio.wait_for(both_downstream_entered.wait(), timeout=3)
        assert observed_contexts == {
            request_id: request_id for request_id in request_ids
        }
        assert current_request_id() is None
        release_downstreams.set()
        responses = await asyncio.wait_for(
            asyncio.gather(*tasks),
            timeout=3,
        )
    finally:
        release_downstreams.set()
        if any(not task.done() for task in tasks):
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    assert {
        response.headers["X-Request-ID"] for response in responses
    } == set(request_ids)
    assert current_request_id() is None
    assert (
        runtime_metrics.snapshot()["counters"]["http_requests_total"]
        == metrics_before + 2
    )


def test_json_logs_include_request_id_and_redact_sensitive_fields():
    formatter = JsonFormatter()
    token = bind_request_id("req-observability-test")
    try:
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="security.test",
            args=(),
            exc_info=None,
        )
        record.event = "security.test"
        record.fields = {
            "user_id": 7,
            "Authorization": "Bearer should-not-leak",
            "raw_payload": {"account": "secret-provider-data"},
            "password": "never-log-me",
        }
        payload = json.loads(formatter.format(record))
    finally:
        from app.observability import reset_request_id
        reset_request_id(token)

    assert payload["request_id"] == "req-observability-test"
    assert payload["user_id"] == 7
    assert payload["Authorization"] == "<redacted>"
    assert payload["raw_payload"] == "<redacted>"
    assert payload["password"] == "<redacted>"
    serialized = json.dumps(payload)
    assert "should-not-leak" not in serialized
    assert "secret-provider-data" not in serialized
    assert "never-log-me" not in serialized


def test_liveness_and_readiness_are_public_and_report_migrated_database(client):
    live = client.get("/health/live")
    ready = client.get("/health/ready")

    assert live.status_code == 200
    assert live.json() == {"status": "ok"}
    assert ready.status_code == 200
    assert ready.json()["ready"] is True
    assert ready.json()["database"] == "ok"
    assert ready.json()["schema"] == "ok"


def test_readiness_fails_closed_without_leaking_database_error(client, monkeypatch):
    import app.routers.observability as observability_router

    monkeypatch.setattr(
        observability_router,
        "check_database_readiness",
        lambda: {
            "ready": False,
            "database": "error",
            "schema": "unknown",
        },
    )
    response = client.get("/health/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["detail"]["ready"] is False
    assert "postgresql://" not in response.text
    assert "password" not in response.text.lower()


def test_metrics_endpoint_is_authenticated_and_exposes_only_operational_counts(client):
    unauthorized = client.get("/observability/metrics")
    assert unauthorized.status_code == 401

    _, headers = _register_and_login(client, "metrics")
    response = client.get("/observability/metrics", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert "runtime" in body
    assert "durable" in body
    assert body["durable"]["users_total"] == 1
    assert "total_income" not in response.text
    assert "net_worth" not in response.text
    assert "raw_payload" not in response.text


def test_reconciliation_mismatch_emits_runtime_and_durable_signal(client):
    user_id, headers = _register_and_login(client, "recon")
    db = SessionLocal()
    try:
        account = (
            db.query(FinancialAccount)
            .filter(
                FinancialAccount.user_id == user_id,
                FinancialAccount.account_type == "CASH",
                FinancialAccount.is_default.is_(True),
            )
            .one()
        )
        case = detect_cash_reconciliation(
            db,
            user_id=user_id,
            account_id=account.id,
            observed_balance=Decimal("100.00"),
            actor_user_id=user_id,
        )
        assert case.status == "MISMATCH"
        db.commit()
    finally:
        db.close()

    assert (
        runtime_metrics.snapshot()["counters"][
            "reconciliation_mismatches_detected_total"
        ]
        == 1
    )

    response = client.get("/observability/metrics", headers=headers)
    assert response.status_code == 200
    assert response.json()["durable"]["open_reconciliation_mismatches"] == 1


def test_projection_stale_read_is_visible_without_changing_projection_semantics(client):
    user_id, headers = _register_and_login(client, "projection")
    db = SessionLocal()
    try:
        rebuild_user_projection(db, user_id)
    finally:
        db.close()

    created = client.post(
        "/transactions",
        json={
            "amount": "25000.00",
            "description": "make projection stale",
            "category": "test",
            "type": "expense",
        },
        headers={**headers, "Idempotency-Key": uuid4().hex},
    )
    assert created.status_code == 200, created.text

    db = SessionLocal()
    try:
        with pytest.raises(ProjectionStaleError):
            require_fresh_projection(db, user_id)
    finally:
        db.close()

    counters = runtime_metrics.snapshot()["counters"]
    assert counters["projection_rebuilds_completed_total"] == 1
    assert counters["projection_stale_reads_total"] == 1


def test_provider_sync_adapter_failure_is_counted_without_logging_provider_payload(client):
    user_id, headers = _register_and_login(client, "provider")
    created = client.post(
        "/provider-connections",
        json={
            "provider_name": "test-bank",
            "external_account_id": "account-1",
            "display_name": "Test",
        },
        headers={**headers, "Idempotency-Key": uuid4().hex},
    )
    assert created.status_code == 200, created.text
    connection_id = created.json()["id"]

    class FailingAdapter:
        def fetch_page(self, cursor):
            raise RuntimeError("provider network unavailable")

    with pytest.raises(RuntimeError, match="provider network unavailable"):
        sync_provider_connection(
            SessionLocal,
            user_id=user_id,
            connection_id=connection_id,
            adapter=FailingAdapter(),
        )

    counters = runtime_metrics.snapshot()["counters"]
    assert counters["provider_sync_failures_total"] == 1


def test_http_metrics_use_route_templates_not_user_supplied_object_ids(client):
    _, headers = _register_and_login(client, "route")
    runtime_metrics.reset_for_tests()

    response = client.get("/transactions/987654321", headers=headers)
    assert response.status_code == 404

    rows = runtime_metrics.snapshot()["http"]
    assert any(
        row["route"] == "/transactions/{transaction_id}"
        and row["status_code"] == 404
        for row in rows
    )
    assert all("987654321" not in row["route"] for row in rows)


def test_durable_snapshot_exposes_checkpoint_age_not_a_fake_stuck_claim(client):
    _register_and_login(client, "durable")
    db = SessionLocal()
    try:
        snapshot = durable_operational_snapshot(db)
    finally:
        db.close()

    assert "provider_sync_checkpoint_oldest_update_age_seconds" in snapshot
    assert all("stuck" not in key for key in snapshot)
