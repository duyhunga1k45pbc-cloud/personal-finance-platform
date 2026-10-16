from uuid import uuid4

from fastapi.testclient import TestClient

from app.main import app
from app.routers.activity_stream import parse_cursor

client = TestClient(app)


def create_user():
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


def test_sse_requires_authentication():
    response = client.get("/activity/stream")
    assert response.status_code == 401


def test_sse_rejects_invalid_cursor_before_streaming():
    _, headers = create_user()

    response = client.get(
        "/activity/stream",
        headers={**headers, "Last-Event-ID": "-1"},
    )

    assert response.status_code == 400


def test_sse_rejects_future_cursor_before_streaming():
    _, headers = create_user()

    response = client.get(
        "/activity/stream",
        headers={
            **headers,
            "Last-Event-ID": "9223372036854775807",
        },
    )

    assert response.status_code == 400
