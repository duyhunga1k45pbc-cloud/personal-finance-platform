"""Auth-session lifecycle and transaction-correctness tests (PostgreSQL)."""

import datetime
import json
import threading
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, text

from app.database import Base, SessionLocal, engine
from app.main import app
from app.models import CommandReceipt, ExternalTransaction, ProviderConnection, User
from app.routers import providers

client = TestClient(app)
Base.metadata.create_all(bind=engine)


def create_user():
    email = f"sl_{uuid4().hex}@example.com"
    client.post("/auth/register", json={"email": email, "password": "testpassword123"})
    tok = client.post("/auth/login", json={"email": email, "password": "testpassword123"}).json()["access_token"]
    h = {"Authorization": f"Bearer {tok}"}
    return client.get("/auth/me", headers=h).json()["id"], h


def idem(h, key=None):
    return {**h, "Idempotency-Key": key or uuid4().hex}


def connect(h, account="acct"):
    r = client.post("/provider-connections", json={"provider_name": "b", "external_account_id": account}, headers=idem(h))
    assert r.status_code == 200
    return r.json()["id"]


def ingest(h, cid, ext="t1", key=None, payload="a"):
    return client.post(
        f"/provider-connections/{cid}/external-transactions",
        json={"external_transaction_id": ext, "observed_at": "2026-09-03T00:00:00Z", "raw_payload": {"v": payload}},
        headers=idem(h, key),
    )


class PoolTrace:
    """Records the checkout/checkin order and the peak number of held connections."""

    def __enter__(self):
        self.events, self.cur, self.peak = [], 0, 0
        self.lock = threading.Lock()
        event.listen(engine, "checkout", self._co)
        event.listen(engine, "checkin", self._ci)
        event.listen(engine, "before_cursor_execute", self._ex)
        return self

    def __exit__(self, *a):
        for name, fn in (("checkout", self._co), ("checkin", self._ci), ("before_cursor_execute", self._ex)):
            event.remove(engine, name, fn)

    def _co(self, *a):
        with self.lock:
            self.cur += 1; self.peak = max(self.peak, self.cur); self.events.append("checkout")

    def _ci(self, *a):
        with self.lock:
            self.cur -= 1; self.events.append("checkin")

    def _ex(self, conn, cur, stmt, *a):
        self.events.append("users" if "FROM users" in stmt and "provider" not in stmt else "sql")


def test_auth_connection_is_returned_before_the_handler_needs_one():
    _, h = create_user()
    cid = connect(h)
    with PoolTrace() as t:
        assert client.get(f"/provider-connections/{cid}/external-transactions", headers=h).status_code == 200
    assert t.peak == 1, t.events
    assert t.events[:3] == ["checkout", "users", "checkin"], t.events  # auth query, then release
    assert t.events.count("users") == 1  # no duplicate authentication query


def test_pool_of_one_connection_is_enough_for_a_request():
    from sqlalchemy import create_engine
    import app.database as database
    tiny = create_engine(database.DATABASE_URL, pool_size=1, max_overflow=0, pool_timeout=2)
    original = SessionLocal.kw["bind"]
    SessionLocal.configure(bind=tiny)
    try:
        _, h = create_user()
        cid = connect(h)
        assert client.get(f"/provider-connections/{cid}/external-transactions", headers=h).status_code == 200
        assert ingest(h, cid).status_code == 200
    finally:
        SessionLocal.configure(bind=original)
        tiny.dispose()


def test_auth_contracts_unchanged():
    _, ha = create_user()
    _, hb = create_user()
    cid = connect(ha)
    url = f"/provider-connections/{cid}/external-transactions"
    assert client.get(url).status_code == 401
    assert client.get(url).headers["www-authenticate"] == "Bearer"
    assert client.get(url, headers={"Authorization": "Bearer junk"}).status_code == 401
    assert client.get(url, headers={"Authorization": "Basic abc"}).status_code == 401
    assert client.get(url, headers=hb).status_code == 404  # cross-user
    assert client.get(f"/provider-connections/{cid + 10**7}/external-transactions", headers=ha).status_code == 404


def test_deleted_principal_is_rejected_and_no_connection_leaks():
    uid, h = create_user()
    with engine.begin() as c:
        c.execute(text("delete from financial_accounts where user_id=:u"), {"u": uid})
        c.execute(text("delete from users where id=:u"), {"u": uid})
    with PoolTrace() as t:
        assert client.get("/provider-connections/1/external-transactions", headers=h).status_code == 401
    assert t.cur == 0 and t.peak == 1


def test_write_then_handler_database_exception_rolls_back_and_releases(monkeypatch):
    uid, h = create_user()
    cid = connect(h)

    def boom(db, **kw):
        db.add(ExternalTransaction(user_id=uid, provider_connection_id=cid, external_transaction_id="X",
                                   first_observed_at=datetime.datetime.now(datetime.timezone.utc)))
        db.flush()
        raise RuntimeError("handler failure")

    monkeypatch.setattr(providers, "ingest_external_evidence", boom)
    with PoolTrace() as t:
        r = TestClient(app, raise_server_exceptions=False).post(
            f"/provider-connections/{cid}/external-transactions",
            json={"external_transaction_id": "X", "observed_at": "2026-09-03T00:00:00Z", "raw_payload": {}}, headers=idem(h))
    assert r.status_code == 500
    assert t.cur == 0
    with SessionLocal() as db:
        assert db.query(ExternalTransaction).filter_by(provider_connection_id=cid).count() == 0
        assert db.query(CommandReceipt).filter_by(user_id=uid, command_type="INGEST_EXTERNAL_TRANSACTION").count() == 0
    assert client.get("/auth/me", headers=h).status_code == 200  # auth state intact


def test_idempotent_replay_and_conflicting_payload_contracts():
    _, h = create_user()
    cid = connect(h)
    first = ingest(h, cid, key="k1")
    again = ingest(h, cid, key="k1")
    assert first.status_code == again.status_code == 200 and first.json() == again.json()
    conflict = ingest(h, cid, key="k1", payload="different")
    assert conflict.status_code == 409


def test_concurrent_idempotent_ingest_creates_one_row():
    uid, h = create_user()
    cid = connect(h)
    results = []
    barrier = threading.Barrier(6)

    def go():
        local = TestClient(app)
        barrier.wait(10)
        results.append(local.post(f"/provider-connections/{cid}/external-transactions",
                                  json={"external_transaction_id": "same", "observed_at": "2026-09-03T00:00:00Z", "raw_payload": {"v": 1}},
                                  headers=idem(h, "shared-key")).status_code)

    ts = [threading.Thread(target=go) for _ in range(6)]
    [t.start() for t in ts]; [t.join(30) for t in ts]
    assert sorted(set(results)) == [200]
    with SessionLocal() as db:
        assert db.query(ExternalTransaction).filter_by(provider_connection_id=cid).count() == 1


def test_all_provider_routes_use_the_same_principal_for_guard_and_handler():
    _, h = create_user()
    cid = connect(h)
    with PoolTrace() as t:
        r = client.get(f"/provider-connections/{cid}", headers=h)  # guard + authorize_object + handler
    assert r.status_code == 200 and t.events.count("users") == 1 and t.peak == 1


def _flatten(dep, acc):
    acc.append(dep.call)
    for sub in dep.dependencies:
        _flatten(sub, acc)
    return acc


def test_no_protected_route_keeps_the_overlapping_auth_session():
    from app.authorization import ROUTE_POLICIES, Access
    from app.routers import auth
    from tests.test_authorization_policy import _all_routes, _route_keys

    owner_routes = 0
    for route in _all_routes():
        calls = _flatten(route.dependant, [])
        key = _route_keys(route)[0]
        if ROUTE_POLICIES[key].access is Access.OWNER:
            owner_routes += 1
            assert auth.get_principal in calls, key
            assert auth.get_current_user not in calls, key
            assert auth.authorize_owner_route not in calls, key
            assert auth.get_db not in calls, key  # the only auth.get_db user is get_current_user
    assert owner_routes >= 30


def test_converted_routers_hold_one_connection_at_a_time_and_release_on_errors():
    _, h = create_user()
    _, other = create_user()
    with PoolTrace() as t:
        statuses = [
            client.get("/accounts", headers=h).status_code,
            client.get("/transactions", headers=h).status_code,
            client.get("/projections/accounts", headers=h).status_code,
            client.get("/projections/summary", headers=h).status_code,
            client.get("/summary", headers=h).status_code,
            client.get("/accounts/999999999", headers=h).status_code,  # 404 path
            client.post("/transactions", json={}, headers=idem(h)).status_code,  # 422 path
            client.get("/accounts", headers=other).status_code,
            client.get("/accounts").status_code,  # 401 path, no connection at all
        ]
    assert statuses[-1] == 401 and 422 in statuses and 404 in statuses
    assert all(s in (200, 404, 409, 422) for s in statuses[:-1]), statuses
    assert t.peak == 1 and t.cur == 0, (t.peak, t.cur, statuses)
