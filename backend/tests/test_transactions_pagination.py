"""GET /transactions pagination: explicit ORDER BY id ASC, PostgreSQL-backed."""

import datetime
from uuid import uuid4

from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app.models import Transaction

client = TestClient(app)


def _user():
    email = f"page_{uuid4().hex}@example.com"
    client.post("/auth/register", json={"email": email, "password": "testpassword123"})
    token = client.post("/auth/login", json={"email": email, "password": "testpassword123"}).json()["access_token"]
    return {"Authorization": "Bearer " + token}


def _create(headers, n, tag="t", type_="expense"):
    for i in range(n):
        r = client.post(
            "/transactions",
            json={"amount": "10.00", "description": f"{tag}{i}", "category": "c", "type": type_},
            headers={**headers, "Idempotency-Key": uuid4().hex},
        )
        assert r.status_code in (200, 201), r.text


def _set_dates(user_headers, fn):
    ids = [t["id"] for t in client.get("/transactions?limit=100", headers=user_headers).json()["data"]]
    db = SessionLocal()
    try:
        for tid in reversed(ids):
            db.query(Transaction).filter(Transaction.id == tid).update({"date": fn(tid)})
        db.commit()
    finally:
        db.close()
    return ids


def _pages(headers, limit, query=""):
    out, skip = [], 0
    while True:
        page = client.get(f"/transactions?limit={limit}&skip={skip}{query}", headers=headers).json()
        if not page["data"]:
            return out, page["total"]
        out += [t["id"] for t in page["data"]]
        skip += limit


def test_order_is_id_ascending_even_when_dates_disagree():
    h = _user()
    _create(h, 7)
    base = datetime.datetime(2024, 1, 1)
    _set_dates(h, lambda tid: base - datetime.timedelta(days=tid))  # newer id => older date
    ids = [t["id"] for t in client.get("/transactions?limit=100", headers=h).json()["data"]]
    assert ids == sorted(ids) and len(ids) == 7


def test_pages_have_no_gaps_or_duplicates_with_identical_dates():
    h = _user()
    _create(h, 11)
    same = datetime.datetime(2024, 5, 5, 12, 0, 0)
    all_ids = _set_dates(h, lambda tid: same)
    for limit in (1, 3, 4, 10, 11, 100):
        paged, total = _pages(h, limit)
        assert paged == sorted(all_ids) and len(set(paged)) == len(paged) == total == 11
    # Repeating the same request returns the same rows.
    first = client.get("/transactions?limit=4&skip=4", headers=h).json()
    assert first == client.get("/transactions?limit=4&skip=4", headers=h).json()
    assert [t["id"] for t in first["data"]] == sorted(all_ids)[4:8]


def test_boundaries_total_and_filters_preserved():
    h = _user()
    _create(h, 5, "e", "expense")
    _create(h, 3, "i", "income")
    page = client.get("/transactions?limit=3&skip=6", headers=h).json()
    assert (page["total"], page["skip"], page["limit"]) == (8, 6, 3)
    assert len(page["data"]) == 2
    assert client.get("/transactions?skip=8", headers=h).json()["data"] == []
    assert client.get("/transactions?limit=0", headers=h).status_code == 422
    assert client.get("/transactions?limit=101", headers=h).status_code == 422
    assert client.get("/transactions?skip=-1", headers=h).status_code == 422
    inc, total = _pages(h, 2, "&type=income")
    assert total == 3 and len(inc) == 3 and inc == sorted(inc)
    assert set(page["data"][0]) == {"id", "category", "type", "account_id", "amount", "description", "date", "user_id"}


def test_pagination_isolated_per_user():
    a, b = _user(), _user()
    _create(a, 4, "a")
    _create(b, 6, "b")
    a_ids, a_total = _pages(a, 2)
    b_ids, b_total = _pages(b, 4)
    assert a_total == 4 and b_total == 6
    assert not set(a_ids) & set(b_ids)
    assert {t["description"][0] for t in client.get("/transactions?limit=100", headers=a).json()["data"]} == {"a"}


def test_unauthenticated_unchanged():
    assert client.get("/transactions?limit=3&skip=0").status_code == 401
