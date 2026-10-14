"""Moneta demo page: public static shell; all data stays behind the authorized APIs."""

from uuid import uuid4

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _user():
    email = f"demo_{uuid4().hex}@example.com"
    client.post("/auth/register", json={"email": email, "password": "testpassword123"})
    token = client.post("/auth/login", json={"email": email, "password": "testpassword123"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_demo_page_is_public_and_carries_no_data():
    r = client.get("/demo")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert r.headers["cache-control"] == "no-store"
    html = r.text
    assert "moneta" in html and "/auth/login" in html
    assert "localStorage" not in html
    # sessionStorage is permitted for short-lived PKCE transaction state.
    assert "sessionStorage.setItem(OIDC_PENDING" in html
    assert "sessionStorage.removeItem(OIDC_PENDING)" in html
    assert "sessionStorage.setItem('access_token'" not in html
    assert "sessionStorage.setItem('refresh_token'" not in html
    assert "Authorization" in html


def test_demo_does_not_weaken_api_authorization():
    for path in ("/accounts", "/transactions", "/summary", "/projections/accounts", "/reconciliations/1"):
        assert client.get(path).status_code == 401
    assert client.post("/demo").status_code in (403, 405)


def test_empty_user_gets_empty_lists_and_zero_summary():
    h = _user()
    assert client.get("/transactions", headers=h).json()["data"] == []
    assert client.get("/summary", headers=h).json() == {"total_income": 0, "total_expense": 0, "balance": 0}
    assert len(client.get("/accounts", headers=h).json()) == 1  # default cash account


def test_data_shown_by_the_ui_is_only_the_callers():
    a, b = _user(), _user()
    r = client.post(
        "/transactions",
        json={"amount": "10.00", "description": "<script>alert(1)</script>", "category": "x", "type": "expense"},
        headers={**a, "Idempotency-Key": uuid4().hex},
    )
    assert r.status_code == 200
    assert client.get("/transactions", headers=b).json()["total"] == 0
    mine = client.get("/transactions", headers=a).json()
    assert mine["total"] == 1 and mine["data"][0]["description"] == "<script>alert(1)</script>"
    assert {t["user_id"] for t in mine["data"]} == {mine["data"][0]["user_id"]}
    html = client.get("/demo").text
    assert "escapeHtml(x.description" in html  # backend text is escaped before insertion
