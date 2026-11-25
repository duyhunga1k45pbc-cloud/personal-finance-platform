from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.analysis_context import build_analysis_context
from app.main import app, close_ai_service_http_client, start_ai_service_http_client
from app.routers import analytics
from app.routers.auth import Principal, get_principal


TEST_TOKEN = "test-only-internal-token-value-at-least-32-chars"
FINGERPRINT = "a" * 64


def context_for(user_id: int, generation: int = 7, fingerprint: str = FINGERPRINT):
    state = SimpleNamespace(
        total_income=Decimal("100.00") + user_id,
        total_expense=Decimal("25.00"),
        economic_balance=Decimal("75.00") + user_id,
        net_worth=Decimal("75.00") + user_id,
        account_count=1,
        generation=generation,
        canonical_fingerprint=fingerprint,
        rebuilt_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    return build_analysis_context(
        user_id=user_id,
        state=state,
        checked_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )


@pytest.fixture
def insights_setup(monkeypatch):
    previous_overrides = app.dependency_overrides.copy()
    app.state.test_user_id = 101
    forwarded = []
    app.state.test_go_handler = lambda _request: httpx.Response(
        200,
        json={"summary": "trusted", "findings": [], "limitations": []},
    )

    def principal_override():
        return Principal(
            id=app.state.test_user_id,
            email=f"user-{app.state.test_user_id}@example.test",
        )

    app.dependency_overrides[get_principal] = principal_override
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: (
                forwarded.append(request)
                or app.state.test_go_handler(request)
            )
        )
    )
    app.dependency_overrides[analytics.get_ai_http_client] = lambda: client
    monkeypatch.setattr(
        analytics, "_build_fresh_context", lambda user_id: context_for(user_id)
    )
    monkeypatch.setattr(
        analytics,
        "_fresh_projection_identity",
        lambda user_id: (context_for(user_id)["generation"], FINGERPRINT),
    )
    monkeypatch.setenv("PFP_AI_INTERNAL_TOKEN", TEST_TOKEN)
    monkeypatch.setenv("PFP_AI_SERVICE_URL", "http://go-test:8091")
    test_client = TestClient(app)
    try:
        yield test_client, client, forwarded
    finally:
        test_client.close()
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous_overrides)
        asyncio.run(client.aclose())
        if hasattr(app.state, "test_go_handler"):
            del app.state.test_go_handler
        if hasattr(app.state, "test_user_id"):
            del app.state.test_user_id


def test_owner_success_isolation_and_exact_internal_forwarding(insights_setup):
    client, _http_client, forwarded = insights_setup
    response_a = client.post(
        "/analytics/insights", headers={"Authorization": "Bearer user-a-private-jwt"}
    )
    assert response_a.status_code == 200
    result_a = response_a.json()
    sent_a = json.loads(forwarded[-1].content)
    assert sent_a == {"context": context_for(101)}
    assert forwarded[-1].url == "http://go-test:8091/internal/analyze"
    assert forwarded[-1].headers["X-Internal-Token"] == TEST_TOKEN
    assert "authorization" not in forwarded[-1].headers
    assert "user-a-private-jwt" not in forwarded[-1].content.decode()
    assert result_a["snapshot_id"] == sent_a["context"]["snapshot_id"]
    assert result_a["schema_version"] == 2
    assert result_a["freshness"]["generation"] == sent_a["context"]["generation"]
    assert result_a["freshness"]["canonical_fingerprint"] == FINGERPRINT
    assert result_a["analysis"]["summary"] == "trusted"

    app.state.test_user_id = 202
    response_b = client.post("/analytics/insights")
    assert response_b.status_code == 200
    sent_b = json.loads(forwarded[-1].content)["context"]
    assert sent_b == context_for(202)
    assert sent_b["snapshot_id"] != sent_a["context"]["snapshot_id"]
    assert response_b.json()["snapshot_id"] == sent_b["snapshot_id"]


def test_real_authenticated_owners_forward_only_their_fresh_projection(monkeypatch):
    forwarded = []

    def fake_go(request):
        forwarded.append(request)
        return httpx.Response(
            200,
            json={"summary": "trusted", "findings": [], "limitations": []},
        )

    internal_client = httpx.AsyncClient(transport=httpx.MockTransport(fake_go))
    previous = app.dependency_overrides.get(analytics.get_ai_http_client)
    app.dependency_overrides[analytics.get_ai_http_client] = lambda: internal_client
    monkeypatch.setenv("PFP_AI_INTERNAL_TOKEN", TEST_TOKEN)
    monkeypatch.setenv("PFP_AI_SERVICE_URL", "http://go-test:8091")
    api = TestClient(app)
    headers_by_owner = []

    try:
        for label, amount in (("insights-a", "111.00"), ("insights-b", "222.00")):
            email = f"{label}-{uuid4().hex}@example.com"
            password = "testpassword123"
            assert api.post("/auth/register", json={"email": email, "password": password}).status_code == 200
            login = api.post("/auth/login", json={"email": email, "password": password})
            assert login.status_code == 200
            headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
            headers_by_owner.append(headers)
            transaction = api.post(
                "/transactions",
                headers={**headers, "Idempotency-Key": uuid4().hex},
                json={
                    "amount": amount,
                    "type": "income",
                    "description": "must not be forwarded",
                    "category": "test",
                },
            )
            assert transaction.status_code == 200, transaction.text
            rebuild = api.post("/projections/rebuild", headers=headers)
            assert rebuild.status_code == 200, rebuild.text

        responses = [api.post("/analytics/insights", headers=headers) for headers in headers_by_owner]
        assert [response.status_code for response in responses] == [200, 200]
        sent_contexts = [json.loads(request.content)["context"] for request in forwarded]
        assert [context["metrics"]["total_income"] for context in sent_contexts] == [
            "111.00",
            "222.00",
        ]
        assert sent_contexts[0]["snapshot_id"] != sent_contexts[1]["snapshot_id"]
        for request, response, context in zip(forwarded, responses, sent_contexts):
            assert request.headers["X-Internal-Token"] == TEST_TOKEN
            assert "authorization" not in request.headers
            assert "access_token" not in request.content.decode()
            assert "must not be forwarded" not in request.content.decode()
            assert response.json()["snapshot_id"] == context["snapshot_id"]
            assert response.json()["freshness"]["canonical_fingerprint"] == context[
                "freshness"
            ]["canonical_fingerprint"]
    finally:
        api.close()
        if previous is None:
            app.dependency_overrides.pop(analytics.get_ai_http_client, None)
        else:
            app.dependency_overrides[analytics.get_ai_http_client] = previous
        asyncio.run(internal_client.aclose())


def test_client_cannot_supply_context_snapshot_or_user_id(insights_setup):
    client, _http_client, forwarded = insights_setup
    before = len(forwarded)
    response = client.post(
        "/analytics/insights",
        json={
            "context": context_for(999),
            "snapshot_id": context_for(999)["snapshot_id"],
            "user_id": 999,
        },
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "request_body_not_allowed"
    assert len(forwarded) == before


@pytest.mark.parametrize("detail", ["projection_missing", "projection_stale"])
def test_missing_or_stale_projection_is_rejected_before_go(
    insights_setup, monkeypatch, detail
):
    client, _http_client, forwarded = insights_setup

    def unavailable(_user_id):
        raise HTTPException(status_code=409, detail=detail)

    monkeypatch.setattr(analytics, "_build_fresh_context", unavailable)
    before = len(forwarded)
    response = client.post("/analytics/insights")
    assert response.status_code == 409
    assert response.json()["detail"] == detail
    assert len(forwarded) == before


def test_changed_projection_discards_go_analysis(insights_setup, monkeypatch):
    client, _http_client, _forwarded = insights_setup
    monkeypatch.setattr(
        analytics,
        "_fresh_projection_identity",
        lambda _user_id: (8, "b" * 64),
    )
    response = client.post("/analytics/insights")
    assert response.status_code == 409
    assert response.json()["detail"] == "analysis_snapshot_changed"


@pytest.mark.parametrize(
    ("status", "headers", "expected_status", "expected_detail"),
    [
        (503, {"Retry-After": "1"}, 503, "analysis_overloaded"),
        (504, {}, 504, "analysis_timeout"),
        (502, {}, 502, "analysis_service_failure"),
    ],
)
def test_go_statuses_are_sanitized(
    insights_setup, status, headers, expected_status, expected_detail
):
    client, _http_client, _forwarded = insights_setup
    app.state.test_go_handler = lambda _request: httpx.Response(
        status, headers=headers, json={"error": "private downstream detail"}
    )
    response = client.post("/analytics/insights")
    assert response.status_code == expected_status
    assert response.json()["detail"] == expected_detail
    assert "private downstream detail" not in response.text
    if status == 503:
        assert response.headers["Retry-After"] == "1"


def test_malformed_go_response_maps_to_502(insights_setup):
    client, _http_client, _forwarded = insights_setup
    app.state.test_go_handler = lambda _request: httpx.Response(200, content=b"not-json")
    response = client.post("/analytics/insights")
    assert response.status_code == 502
    assert response.json()["detail"] == "analysis_invalid_response"


def test_go_network_and_timeout_failures_are_sanitized(insights_setup):
    client, http_client, _forwarded = insights_setup
    transport = httpx.MockTransport(
        lambda _request: (_ for _ in ()).throw(httpx.ConnectError("private network detail"))
    )
    asyncio.run(http_client.aclose())
    replacement = httpx.AsyncClient(transport=transport)
    app.dependency_overrides[analytics.get_ai_http_client] = lambda: replacement
    response = client.post("/analytics/insights")
    assert response.status_code == 502
    assert response.json()["detail"] == "analysis_service_unavailable"
    assert "private network detail" not in response.text
    asyncio.run(replacement.aclose())

    timeout_transport = httpx.MockTransport(
        lambda _request: (_ for _ in ()).throw(httpx.ReadTimeout("private timeout detail"))
    )
    timeout_client = httpx.AsyncClient(transport=timeout_transport)
    app.dependency_overrides[analytics.get_ai_http_client] = lambda: timeout_client
    timed_out = client.post("/analytics/insights")
    assert timed_out.status_code == 504
    assert timed_out.json()["detail"] == "analysis_timeout"
    assert "private timeout detail" not in timed_out.text
    asyncio.run(timeout_client.aclose())


class DisconnectingRequest:
    async def stream(self):
        yield b""

    async def is_disconnected(self):
        await self.transport_started.wait()
        return True


def test_client_disconnect_cancels_go_call_and_lifecycle_closes_client(
    insights_setup, monkeypatch
):
    _test_client, _fake_client, _forwarded = insights_setup
    started = asyncio.Event()
    canceled = asyncio.Event()

    class BlockingTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                canceled.set()
                raise

    request = DisconnectingRequest()
    request.transport_started = started
    client = httpx.AsyncClient(transport=BlockingTransport())

    async def run_cancelled_call():
        with pytest.raises(HTTPException) as error:
            await analytics.get_financial_insights(
                request,
                Principal(id=101, email="owner@example.test"),
                client,
            )
        assert error.value.status_code == 499
        assert canceled.is_set()
        await client.aclose()

    asyncio.run(run_cancelled_call())

    async def lifecycle():
        await start_ai_service_http_client()
        managed = app.state.ai_http_client
        assert not managed.is_closed
        await close_ai_service_http_client()
        return managed.is_closed

    assert asyncio.run(lifecycle())


def test_missing_bearer_auth_is_rejected(insights_setup):
    client, _http_client, forwarded = insights_setup
    previous = app.dependency_overrides.pop(get_principal)
    try:
        response = client.post("/analytics/insights")
    finally:
        app.dependency_overrides[get_principal] = previous
    assert response.status_code == 401
    assert not forwarded
