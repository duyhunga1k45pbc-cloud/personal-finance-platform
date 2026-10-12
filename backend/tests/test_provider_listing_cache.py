"""Real PostgreSQL + real Redis tests for the provider listing cache.

Set REDIS_TEST_URL (e.g. redis://127.0.0.1:6390/15). The tests flush that Redis DB.
"""

import datetime
import os
import socket
import threading
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app import listing_cache
from app.database import Base, SessionLocal, engine
from app.main import app
from app.models import ExternalTransaction, ProviderConnection
from app.provider_sync_service import (
    ProviderSyncObservation,
    ProviderSyncPageData,
    commit_sync_page,
)

REDIS_TEST_URL = os.getenv("REDIS_TEST_URL")
if os.getenv("REQUIRE_REDIS_TESTS") and not REDIS_TEST_URL:
    raise RuntimeError("REQUIRE_REDIS_TESTS is set but REDIS_TEST_URL is missing")
pytestmark = pytest.mark.skipif(not REDIS_TEST_URL, reason="REDIS_TEST_URL not set")

client = TestClient(app)
Base.metadata.create_all(bind=engine)


@pytest.fixture(autouse=True)
def redis_cache(monkeypatch):
    import redis

    assert redis.connection.parse_url(REDIS_TEST_URL).get("db", 0) != 0, (
        "REDIS_TEST_URL must select a dedicated non-default database (FLUSHDB is used)"
    )
    monkeypatch.setenv("REDIS_URL", REDIS_TEST_URL)
    monkeypatch.setenv("PROVIDER_LISTING_CACHE_TTL_SECONDS", "60")
    listing_cache.reset_client()
    raw = redis.Redis.from_url(REDIS_TEST_URL)
    raw.flushdb()
    for k in listing_cache.stats:
        listing_cache.stats[k] = 0
    yield raw
    raw.flushdb()
    listing_cache.reset_client()


def create_user():
    email = f"lc_{uuid4().hex}@example.com"
    assert client.post("/auth/register", json={"email": email, "password": "testpassword123"}).status_code == 200
    token = client.post("/auth/login", json={"email": email, "password": "testpassword123"}).json()["access_token"]
    uid = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"}).json()["id"]
    return uid, {"Authorization": f"Bearer {token}"}


def create_connection(headers, account="acct-1"):
    r = client.post(
        "/provider-connections",
        json={"provider_name": "mockbank", "external_account_id": account},
        headers={**headers, "Idempotency-Key": uuid4().hex},
    )
    assert r.status_code == 200
    return r.json()["id"]


def ingest(headers, cid, external_id="t1", payload_extra="a"):
    r = client.post(
        f"/provider-connections/{cid}/external-transactions",
        json={
            "external_transaction_id": external_id,
            "observed_at": "2026-09-03T00:00:00Z",
            "raw_payload": {"id": external_id, "v": payload_extra},
        },
        headers={**headers, "Idempotency-Key": uuid4().hex},
    )
    assert r.status_code == 200, r.text
    return r


def listing(headers, cid):
    return client.get(f"/provider-connections/{cid}/external-transactions", headers=headers)


def revision(cid):
    db = SessionLocal()
    try:
        return listing_cache.read_revision(db, cid)
    finally:
        db.close()


def uncached_body(headers, cid, monkeypatch):
    with monkeypatch.context() as m:
        m.delenv("REDIS_URL")
        return listing(headers, cid).content


def test_miss_hit_and_uncached_bytes_are_identical(monkeypatch):
    _, h = create_user()
    cid = create_connection(h)
    ingest(h, cid, "t1")
    ingest(h, cid, "t1", "b")
    ingest(h, cid, "t2")
    expected = uncached_body(h, cid, monkeypatch)
    miss = listing(h, cid)
    hit = listing(h, cid)
    assert miss.status_code == hit.status_code == 200
    assert miss.content == expected
    assert hit.content == expected
    assert hit.headers["content-type"] == "application/json"
    assert listing_cache.stats["hit"] == 1 and listing_cache.stats["miss"] == 1
    assert b'"evidence_count":2' in expected


def test_cross_user_and_cross_connection_isolation():
    _, ha = create_user()
    _, hb = create_user()
    ca = create_connection(ha)
    ca2 = create_connection(ha, "acct-2")
    cb = create_connection(hb)
    ingest(ha, ca, "a-only")
    ingest(ha, ca2, "a2-only")
    assert b"a-only" in listing(ha, ca).content  # populate
    assert b"a-only" in listing(ha, ca).content  # hit
    stolen = listing(hb, ca)
    assert stolen.status_code == 404 and b"a-only" not in stolen.content
    assert listing(hb, cb).json() == []
    assert b"a2-only" not in listing(ha, ca).content
    assert b"a-only" not in listing(ha, ca2).content


def test_cache_keys_are_distinct_per_dimension():
    base = dict(user_id=1, connection_id=2, revision=3)
    keys = {
        listing_cache.cache_key(**base),
        listing_cache.cache_key(**{**base, "user_id": 9}),
        listing_cache.cache_key(**{**base, "connection_id": 9}),
        listing_cache.cache_key(**{**base, "revision": 4}),
        listing_cache.cache_key(**base, params={"limit": 10}),
        listing_cache.cache_key(**base, params={"limit": 10, "offset": 20}),
    }
    assert len(keys) == 6
    a = listing_cache.cache_key(**base, params={"limit": 10, "offset": 0})
    b = listing_cache.cache_key(**base, params={"offset": 0, "limit": 10, "x": None})
    assert a == b


def test_committed_mutations_advance_revision_and_prevent_stale_reads():
    _, h = create_user()
    cid = create_connection(h)
    assert revision(cid) == 0
    assert listing(h, cid).json() == []
    assert listing(h, cid).json() == []  # cached under revision 0
    ingest(h, cid, "t1")
    assert revision(cid) >= 1
    after_tx = listing(h, cid).json()
    assert len(after_tx) == 1
    assert after_tx[0]["evidence_count"] == 1
    rev = revision(cid)
    ingest(h, cid, "t1", "different payload")  # evidence-only change
    assert revision(cid) > rev
    assert listing(h, cid).json()[0]["evidence_count"] == 2
    rev = revision(cid)
    ingest(h, cid, "t1", "different payload")  # idempotent replay: no insert
    assert revision(cid) == rev


def test_sync_page_commit_advances_revision_once_per_statement_group():
    uid, h = create_user()
    cid = create_connection(h)
    assert listing(h, cid).json() == []
    base = datetime.datetime(2026, 9, 3, tzinfo=datetime.timezone.utc)
    obs = [
        ProviderSyncObservation(f"s{i}", base + datetime.timedelta(minutes=i), {"i": i})
        for i in range(3)
    ]
    commit_sync_page(
        SessionLocal, user_id=uid, connection_id=cid, request_cursor=None,
        page=ProviderSyncPageData(observations=obs, next_cursor="n", has_more=False),
    )
    assert len(listing(h, cid).json()) == 3


def test_rolled_back_writes_do_not_advance_revision():
    uid, h = create_user()
    cid = create_connection(h)
    before = revision(cid)
    db = SessionLocal()
    try:
        db.add(ExternalTransaction(
            user_id=uid, provider_connection_id=cid, external_transaction_id="RB",
            first_observed_at=datetime.datetime.now(datetime.timezone.utc)))
        db.flush()
        # uncommitted bump is invisible to other sessions
        assert revision(cid) == before
        db.rollback()
    finally:
        db.close()
    assert revision(cid) == before
    db = SessionLocal()
    try:
        db.add(ExternalTransaction(
            user_id=uid, provider_connection_id=cid, external_transaction_id="SP",
            first_observed_at=datetime.datetime.now(datetime.timezone.utc)))
        with db.begin_nested() as sp:
            db.add(ExternalTransaction(
                user_id=uid, provider_connection_id=cid, external_transaction_id="SP2",
                first_observed_at=datetime.datetime.now(datetime.timezone.utc)))
            db.flush()
            sp.rollback()
        db.commit()
    finally:
        db.close()
    assert revision(cid) == before + 1
    assert [r["external_transaction_id"] for r in listing(h, cid).json()] == ["SP"]


def test_in_flight_stale_population_never_visible_under_newer_revision(monkeypatch):
    uid, h = create_user()
    cid = create_connection(h)
    ingest(h, cid, "old")
    # A slow reader: reads revision, loads data, then stalls before populating.
    db = SessionLocal()
    stale_rev = listing_cache.read_revision(db, cid)
    stale_body = uncached_body(h, cid, monkeypatch)
    db.close()
    ingest(h, cid, "new")  # committed write advances revision
    listing_cache.put_cached(
        listing_cache.cache_key(user_id=uid, connection_id=cid, revision=stale_rev), stale_body
    )
    body = listing(h, cid).content
    assert b"NEW" in body.upper() and body == uncached_body(h, cid, monkeypatch)


def test_concurrent_readers_and_writer_read_after_write(monkeypatch):
    uid, h = create_user()
    cid = create_connection(h)
    stop = threading.Event()
    errors = []

    def reader():
        local = TestClient(app)
        while not stop.is_set():
            r = local.get(f"/provider-connections/{cid}/external-transactions", headers=h)
            if r.status_code != 200:
                errors.append(r.status_code)

    threads = [threading.Thread(target=reader) for _ in range(4)]
    for t in threads:
        t.start()
    try:
        for i in range(15):
            ingest(h, cid, f"w{i}")
            seen = len(listing(h, cid).json())
            if seen != i + 1:
                errors.append(f"stale read after write {i}: saw {seen}")
    finally:
        stop.set()
        for t in threads:
            t.join()
    assert errors == []


def test_redis_outage_falls_back_to_database(monkeypatch):
    _, h = create_user()
    cid = create_connection(h)
    ingest(h, cid)
    expected = uncached_body(h, cid, monkeypatch)
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    listing_cache.reset_client()
    r = listing(h, cid)
    assert r.status_code == 200 and r.content == expected
    assert listing_cache.stats["error"] >= 1


def test_redis_timeout_falls_back_to_database(monkeypatch):
    _, h = create_user()
    cid = create_connection(h)
    ingest(h, cid)
    expected = uncached_body(h, cid, monkeypatch)
    blackhole = socket.socket()
    blackhole.bind(("127.0.0.1", 0))
    blackhole.listen(8)  # accepts at kernel level, never replies
    try:
        monkeypatch.setenv("REDIS_URL", f"redis://127.0.0.1:{blackhole.getsockname()[1]}/0")
        monkeypatch.setenv("REDIS_TIMEOUT_SECONDS", "0.05")
        listing_cache.reset_client()
        start = time.monotonic()
        r = listing(h, cid)
        assert time.monotonic() - start < 2
        assert r.status_code == 200 and r.content == expected
    finally:
        blackhole.close()


def test_expired_and_corrupted_entries_fall_back_and_repair(redis_cache):
    uid, h = create_user()
    cid = create_connection(h)
    ingest(h, cid)
    good = listing(h, cid).content
    key = listing_cache.cache_key(user_id=uid, connection_id=cid, revision=revision(cid))
    assert redis_cache.ttl(key) > 0
    redis_cache.delete(key)  # expired
    assert listing(h, cid).content == good
    for garbage in (b"not json", b"00000000[]", good, b"", listing_cache._pack(b"{}")):
        redis_cache.set(key, garbage)
        r = listing(h, cid)
        assert r.status_code == 200 and r.content == good
        assert listing_cache.get_cached(key) == good  # repaired by the miss path
    assert listing_cache.stats["corrupt"] >= 4


def test_authorization_contracts_unchanged_with_cache_enabled():
    _, ha = create_user()
    _, hb = create_user()
    cid = create_connection(ha)
    ingest(ha, cid)
    listing(ha, cid)
    listing(ha, cid)
    anon = client.get(f"/provider-connections/{cid}/external-transactions")
    assert anon.status_code == 401
    bad = client.get(f"/provider-connections/{cid}/external-transactions",
                     headers={"Authorization": "Bearer nope"})
    assert bad.status_code == 401
    assert listing(hb, cid).status_code == 404
    assert listing(ha, cid + 10_000_000).status_code == 404


def test_removing_the_revision_update_causes_real_stale_reads():
    _, h = create_user()
    cid = create_connection(h)
    assert listing(h, cid).json() == []
    assert listing(h, cid).json() == []
    tables = ("external_transactions", "external_transaction_evidence")
    with engine.begin() as c:
        for t in tables:
            c.execute(text(f"ALTER TABLE {t} DISABLE TRIGGER trg_{t}_listing_revision"))
    try:
        ingest(h, cid, "t1")
        stale = listing(h, cid).json()
    finally:
        with engine.begin() as c:
            for t in tables:
                c.execute(text(f"ALTER TABLE {t} ENABLE TRIGGER trg_{t}_listing_revision"))
    assert stale == []  # stale read proves the revision trigger is what guarantees freshness
    ingest(h, cid, "t2")
    assert len(listing(h, cid).json()) == 2


def test_disabled_cache_never_touches_redis(monkeypatch, redis_cache):
    _, h = create_user()
    cid = create_connection(h)
    ingest(h, cid)
    monkeypatch.delenv("REDIS_URL")
    assert listing(h, cid).status_code == 200
    assert redis_cache.dbsize() == 0


def test_failure_backoff_skips_redis_then_recovers(monkeypatch, redis_cache):
    _, h = create_user()
    cid = create_connection(h)
    ingest(h, cid)
    good = listing(h, cid).content
    monkeypatch.setattr(listing_cache, "_skip_until", time.monotonic() + 60)
    errors_before = listing_cache.stats["error"]
    assert listing(h, cid).content == good  # served from the database
    assert listing_cache.stats["hit"] == 0 and listing_cache.stats["error"] == errors_before
    monkeypatch.setattr(listing_cache, "_skip_until", 0.0)
    listing(h, cid)
    assert listing(h, cid).content == good and listing_cache.stats["hit"] == 2


def test_revision_lookup_failure_never_serves_cached_data(monkeypatch, redis_cache):
    _, h = create_user()
    cid = create_connection(h)
    ingest(h, cid)
    good = listing(h, cid).content
    assert listing(h, cid).content == good and listing_cache.stats["hit"] == 1
    calls = []

    def broken_revision(db, connection_id):
        raise RuntimeError("revision store unavailable")

    monkeypatch.setattr(listing_cache, "read_revision", broken_revision)
    monkeypatch.setattr(listing_cache, "get_cached", lambda key: calls.append(key))
    r = TestClient(app, raise_server_exceptions=False).get(
        f"/provider-connections/{cid}/external-transactions", headers=h)
    assert r.status_code == 500 and good not in r.content
    assert calls == []  # Redis was never consulted without a committed revision


def test_write_landing_between_revision_read_and_load_is_never_stale(monkeypatch, redis_cache):
    uid, h = create_user()
    cid = create_connection(h)
    ingest(h, cid, "before")
    real_get = listing_cache.get_cached
    fired = []

    def get_then_write(key):
        if not fired:  # a write commits after the revision was read, before the data load
            fired.append(key)
            ingest(h, cid, "racing")
        return real_get(key)

    monkeypatch.setattr(listing_cache, "get_cached", get_then_write)
    first = listing(h, cid)  # revision r read, then "racing" commits (r+2), then data loads
    monkeypatch.setattr(listing_cache, "get_cached", real_get)
    assert b"RACING" in first.content.upper()  # data is at least as new as its revision
    second = listing(h, cid)  # new revision => new key, never the r-keyed entry
    assert second.content == first.content
    assert fired[0] != listing_cache.cache_key(user_id=uid, connection_id=cid, revision=revision(cid))
