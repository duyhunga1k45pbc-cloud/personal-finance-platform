"""Deterministic PostgreSQL concurrency tests for the listing revision triggers."""

import datetime
import threading
import time
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import text

from app.database import Base, SessionLocal, engine
from app.main import app
from app.models import ExternalTransaction
from app.provider_service import ingest_external_evidence
from app.models import ProviderConnection

client = TestClient(app)
Base.metadata.create_all(bind=engine)
NOW = lambda: datetime.datetime.now(datetime.timezone.utc)


def make_connections(n=1):
    email = f"rc_{uuid4().hex}@example.com"
    client.post("/auth/register", json={"email": email, "password": "testpassword123"})
    token = client.post("/auth/login", json={"email": email, "password": "testpassword123"}).json()["access_token"]
    h = {"Authorization": f"Bearer {token}"}
    uid = client.get("/auth/me", headers=h).json()["id"]
    ids = []
    for i in range(n):
        r = client.post("/provider-connections", json={"provider_name": "b", "external_account_id": f"a{i}"},
                        headers={**h, "Idempotency-Key": uuid4().hex})
        ids.append(r.json()["id"])
    return uid, h, ids


def revision(cid):
    with engine.connect() as c:
        return c.execute(text("select revision from provider_listing_revisions where provider_connection_id=:c"),
                         {"c": cid}).scalar() or 0


def add_tx(db, uid, cid, name):
    db.add(ExternalTransaction(user_id=uid, provider_connection_id=cid,
                               external_transaction_id=name, first_observed_at=NOW()))
    db.flush()


def test_same_connection_writers_serialize_on_the_revision_row_without_deadlock():
    uid, _, (cid,) = make_connections()
    first = SessionLocal()
    add_tx(first, uid, cid, "A")  # holds the revision row lock, uncommitted
    done = threading.Event()
    result = {}

    def second_writer():
        db = SessionLocal()
        try:
            add_tx(db, uid, cid, "B")
            result["visible_to_second_before_commit"] = True
            db.commit()
            result["ok"] = True
        except Exception as exc:  # pragma: no cover - failure path
            result["error"] = repr(exc)
        finally:
            db.close()
            done.set()

    t = threading.Thread(target=second_writer)
    t.start()
    assert not done.wait(0.7), "second writer must block on the revision row until the first commits"
    assert revision(cid) == 0  # nothing visible while both are uncommitted
    first.commit()
    first.close()
    assert done.wait(10) and result.get("ok"), result
    t.join()
    assert revision(cid) == 2


def test_concurrent_first_inserts_and_many_writers_lose_no_bumps():
    uid, _, (cid,) = make_connections()
    n = 10  # stays below the 15-connection pool so the barrier can release
    barrier = threading.Barrier(n)
    errors = []

    def writer(i):
        db = SessionLocal()
        try:
            conn = db.get(ProviderConnection, cid)
            barrier.wait(10)
            ingest_external_evidence(db, user_id=uid, connection=conn, external_transaction_id=f"x{i}",
                                     raw_payload={"i": i}, observed_at=None)
            db.commit()
        except Exception as exc:
            errors.append(repr(exc))
            db.rollback()
        finally:
            db.close()

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(n)]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    assert errors == []
    with engine.connect() as c:
        rows = c.execute(text("select count(*) from external_transactions where provider_connection_id=:c"), {"c": cid}).scalar()
    assert rows == n
    assert revision(cid) == 2 * n  # one bump per inserting statement: transaction + evidence


def test_multi_row_statement_across_connections_locks_in_a_fixed_order():
    uid, _, (c1, c2) = make_connections(2)
    for round_no in range(10):
        barrier = threading.Barrier(2)
        errors = []

        def run(first, second):
            db = SessionLocal()
            try:
                barrier.wait(10)
                db.execute(text(
                    "insert into external_transactions(user_id,provider_connection_id,external_transaction_id,first_observed_at) "
                    "values (:u,:a,:n,now()),(:u,:b,:n,now())"),
                    {"u": uid, "a": first, "b": second, "n": f"r{round_no}{threading.get_ident()}"})
                db.commit()
            except Exception as exc:
                errors.append(repr(exc))
                db.rollback()
            finally:
                db.close()

        ts = [threading.Thread(target=run, args=(c1, c2)), threading.Thread(target=run, args=(c2, c1))]
        [t.start() for t in ts]
        [t.join(30) for t in ts]
        assert errors == [], errors
    assert revision(c1) == 20 and revision(c2) == 20


def test_reader_between_uncommitted_write_and_commit_sees_old_state_then_new():
    uid, h, (cid,) = make_connections()
    url = f"/provider-connections/{cid}/external-transactions"
    writer = SessionLocal()
    add_tx(writer, uid, cid, "PENDING")
    assert client.get(url, headers=h).json() == [] and revision(cid) == 0  # READ COMMITTED: no dirty read
    writer.commit()
    writer.close()
    assert revision(cid) == 1
    assert [r["external_transaction_id"] for r in client.get(url, headers=h).json()] == ["PENDING"]
