import asyncio
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.activity_feed_reader import async_engine
from app.deployment import deployment_state
from app.main import app
from app.routers import activity_stream
from app.routers.activity_stream import parse_cursor


@pytest.fixture
def client():
    lifespan_pool = None
    try:
        deployment_state.reset_for_tests()
        with TestClient(app) as test_client:
            lifespan_pool = async_engine.sync_engine.pool
            yield test_client
    finally:
        if lifespan_pool is not None:
            assert async_engine.sync_engine.pool is not lifespan_pool
            assert lifespan_pool.checkedout() == 0


def create_user(client):
    email = f"sse-{uuid4().hex}@example.com"
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


def test_cursor_validation():
    assert parse_cursor(None) is None
    assert parse_cursor("0") == 0
    assert parse_cursor("42") == 42

    from fastapi import HTTPException

    for invalid in ("-1", "abc", "1.5", " 1", "9223372036854775808"):
        try:
            parse_cursor(invalid)
        except HTTPException as exc:
            assert exc.status_code == 400
        else:
            raise AssertionError(f"Accepted invalid cursor: {invalid!r}")


def test_sse_requires_authentication(client):
    response = client.get("/activity/stream")
    assert response.status_code == 401


def test_sse_rejects_invalid_cursor_before_streaming(client):
    _, headers = create_user(client)

    response = client.get(
        "/activity/stream",
        headers={**headers, "Last-Event-ID": "-1"},
    )

    assert response.status_code == 400


def test_sse_rejects_future_cursor_before_streaming(client):
    _, headers = create_user(client)

    response = client.get(
        "/activity/stream",
        headers={
            **headers,
            "Last-Event-ID": "9223372036854775807",
        },
    )

    assert response.status_code == 400


def test_sse_requests_share_the_testclient_event_loop(client, monkeypatch):
    loops = []
    current_cursor = activity_stream.current_activity_cursor

    async def record_loop(user_id):
        loops.append(asyncio.get_running_loop())
        return await current_cursor(user_id)

    monkeypatch.setattr(activity_stream, "current_activity_cursor", record_loop)
    _, headers = create_user(client)
    lifespan_pool = async_engine.sync_engine.pool

    for _ in range(2):
        response = client.get(
            "/activity/stream",
            headers={
                **headers,
                "Last-Event-ID": "9223372036854775807",
            },
        )
        assert response.status_code == 400
        assert async_engine.sync_engine.pool is lifespan_pool
        assert lifespan_pool.checkedout() == 0

    assert len(loops) == 2
    assert loops[0] is loops[1]


def test_sse_pool_is_disposed_between_distinct_testclient_lifespans(monkeypatch):
    loops = []
    lifespan_pools = []
    current_cursor = activity_stream.current_activity_cursor

    async def record_loop(user_id):
        loops.append(asyncio.get_running_loop())
        return await current_cursor(user_id)

    monkeypatch.setattr(activity_stream, "current_activity_cursor", record_loop)

    for _ in range(2):
        deployment_state.reset_for_tests()
        with TestClient(app) as loop_client:
            lifespan_pool = async_engine.sync_engine.pool
            _, headers = create_user(loop_client)
            response = loop_client.get(
                "/activity/stream",
                headers={
                    **headers,
                    "Last-Event-ID": "9223372036854775807",
                },
            )
            assert response.status_code == 400
            lifespan_pools.append(lifespan_pool)

        assert async_engine.sync_engine.pool is not lifespan_pool
        assert lifespan_pool.checkedout() == 0

    assert len(loops) == 2
    assert loops[0] is not loops[1]
    assert lifespan_pools[0] is not lifespan_pools[1]
