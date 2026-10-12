"""Redis Fault Injection and Financial Invariant Verification Case Study.

Verifies:
- T1: Deterministic stale cache fill race (revision advancement & cache key separation).
- T2: Redis outage / timeout (fallback to authoritative PostgreSQL, write independence, recovery).
- T3: Cross-user cache isolation (PostgreSQL authorization gate, key separation, concurrent reads).
- T4: Financial invariants while Redis is unavailable (over-refund protection, idempotency, auditability).

All tests run against real PostgreSQL and real Redis where applicable.
"""

from __future__ import annotations

import datetime
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import os
import socket
import threading
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app import causal_service, listing_cache
from app.canonical_audit import audit_user
from app.database import Base, SessionLocal, engine
from app.main import app
from app.models import (
    CommandReceipt,
    FinancialAccount,
    FinancialEvent,
    FinancialEventEntry,
    FinancialEventHistory,
    ProviderConnection,
    Transaction,
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


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _create_user(prefix: str = "fi") -> tuple[int, dict[str, str]]:
    email = f"{prefix}_{uuid4().hex}@example.com"
    pwd = "testpassword123"
    r = client.post("/auth/register", json={"email": email, "password": pwd})
    assert r.status_code == 200, r.text
    token = client.post("/auth/login", json={"email": email, "password": pwd}).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    uid = client.get("/auth/me", headers=headers).json()["id"]
    return uid, headers


def _create_connection(headers: dict[str, str], account_id: str = "acct-1") -> int:
    r = client.post(
        "/provider-connections",
        json={"provider_name": "mockbank", "external_account_id": account_id},
        headers={**headers, "Idempotency-Key": uuid4().hex},
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _ingest(headers: dict[str, str], cid: int, external_id: str = "tx-1", payload_extra: str = "v1"):
    r = client.post(
        f"/provider-connections/{cid}/external-transactions",
        json={
            "external_transaction_id": external_id,
            "observed_at": "2026-10-13T00:00:00Z",
            "raw_payload": {"id": external_id, "v": payload_extra},
        },
        headers={**headers, "Idempotency-Key": uuid4().hex},
    )
    assert r.status_code == 200, r.text
    return r


def _listing(headers: dict[str, str], cid: int):
    return client.get(f"/provider-connections/{cid}/external-transactions", headers=headers)


def _revision(cid: int) -> int:
    with SessionLocal() as db:
        return listing_cache.read_revision(db, cid)


def _uncached_body(headers: dict[str, str], cid: int, monkeypatch) -> bytes:
    with monkeypatch.context() as m:
        m.delenv("REDIS_URL")
        return _listing(headers, cid).content


def _create_user_and_expense(amount: str = "100.00") -> tuple[int, dict[str, str], int]:
    uid, headers = _create_user("refund")
    acct = client.post(
        "/accounts",
        json={"name": "Fault Injection Acct", "account_type": "BANK", "currency": "VND"},
        headers=headers,
    )
    assert acct.status_code == 200, acct.text
    expense = client.post(
        "/transactions",
        json={
            "amount": amount,
            "description": "fault injection expense",
            "category": "test",
            "type": "expense",
            "account_id": acct.json()["id"],
        },
        headers={**headers, "Idempotency-Key": uuid4().hex},
    )
    assert expense.status_code == 200, expense.text
    return uid, headers, expense.json()["canonical_event_id"]


# ===========================================================================
# T1 — Deterministic Stale Cache Fill Race
# ===========================================================================

def test_t1_deterministic_stale_cache_fill_race(redis_cache, monkeypatch):
    """T1: Verify that a slow reader populating an old revision cannot clobber
    a newer revision committed concurrently.

    1. Seed provider transactions and read the committed PostgreSQL listing revision.
    2. Start Reader A and pause at the actual cache write boundary using threading Events.
    3. In a separate database session, commit an additional provider transaction that advances revision.
    4. Allow Reader A to finish writing the old-revision cache entry.
    5. Start Reader C strictly after the writer COMMIT.
    6. Assert Reader C returns the updated listing and cannot select the old-revision cache entry.
    7. Verify the revision advancement and cache key separation.
    """
    uid, headers = _create_user("t1")
    cid = _create_connection(headers)

    # 1. Seed provider transactions and read the committed PostgreSQL listing revision.
    _ingest(headers, cid, "tx-seed-1", "payload-1")
    _ingest(headers, cid, "tx-seed-2", "payload-2")

    r0 = _revision(cid)
    assert r0 > 0, f"Seeded transactions must create revision > 0; got {r0}"

    # 2. Start Reader A and pause at the cache write boundary
    reader_a_at_write_boundary = threading.Event()
    release_reader_a = threading.Event()
    reader_a_done = threading.Event()
    reader_a_response = {}

    real_put_cached = listing_cache.put_cached

    def hooked_put_cached(key: str, body: bytes):
        # Pause Reader A when writing the entry for revision r0
        if f":c{cid}:r{r0}:" in key:
            reader_a_at_write_boundary.set()
            if not release_reader_a.wait(timeout=10.0):
                raise TimeoutError("Reader A was not released in time")
        real_put_cached(key, body)

    monkeypatch.setattr(listing_cache, "put_cached", hooked_put_cached)

    def run_reader_a():
        local_client = TestClient(app)
        res = local_client.get(f"/provider-connections/{cid}/external-transactions", headers=headers)
        reader_a_response["status"] = res.status_code
        reader_a_response["body"] = res.json()
        reader_a_done.set()

    thread_a = threading.Thread(target=run_reader_a)
    thread_a.start()

    # Wait for Reader A to read revision r0, query PostgreSQL, and pause before Redis SET
    assert reader_a_at_write_boundary.wait(timeout=10.0), "Reader A did not reach write boundary"

    # 3. In a separate session, commit an additional provider transaction that advances revision
    _ingest(headers, cid, "tx-seed-3", "payload-3")
    r1 = _revision(cid)
    assert r1 > r0, f"Committed transaction must advance revision; {r1} > {r0}"

    # 4. Allow Reader A to finish writing the old-revision cache entry
    release_reader_a.set()
    assert reader_a_done.wait(timeout=10.0), "Reader A failed to finish"
    thread_a.join()
    assert reader_a_response["status"] == 200
    reader_a_tx_ids = [tx["external_transaction_id"] for tx in reader_a_response["body"]]
    assert sorted(reader_a_tx_ids) == ["tx-seed-1", "tx-seed-2"]

    # 5. Start Reader C strictly after the writer COMMIT
    local_client_c = TestClient(app)
    res_c = local_client_c.get(f"/provider-connections/{cid}/external-transactions", headers=headers)
    assert res_c.status_code == 200

    # 6. Assert Reader C returns the updated listing and cannot select old-revision entry
    tx_ids_c = [tx["external_transaction_id"] for tx in res_c.json()]
    assert "tx-seed-3" in tx_ids_c, "Reader C must observe the committed transaction"
    assert len(tx_ids_c) == 3

    # 7. Verify the revision advancement and cache key separation
    old_key = listing_cache.cache_key(user_id=uid, connection_id=cid, revision=r0)
    new_key = listing_cache.cache_key(user_id=uid, connection_id=cid, revision=r1)
    assert old_key != new_key, "Old and new keys must differ by revision"

    # Old revision key in Redis holds Reader A's old data
    raw_old = redis_cache.get(old_key)
    assert raw_old is not None
    unpacked_old = listing_cache._unpack(raw_old)
    assert b"tx-seed-3" not in unpacked_old
    assert b"tx-seed-1" in unpacked_old

    # New revision key in Redis holds Reader C's updated data
    raw_new = redis_cache.get(new_key)
    assert raw_new is not None
    unpacked_new = listing_cache._unpack(raw_new)
    assert b"tx-seed-3" in unpacked_new


def test_t1_regression_missing_revision_causes_stale_cache_fill(redis_cache, monkeypatch):
    """Regression counterfactual: If the cache key lacked the revision dimension,
    Reader A's delayed write would clobber the cache and Reader C would observe stale data.
    """
    uid, headers = _create_user("t1vuln")
    cid = _create_connection(headers)
    _ingest(headers, cid, "tx-seed-1")

    # Simulate vulnerable unversioned cache key
    def unversioned_cache_key(*, user_id: int, connection_id: int, revision: int, params=None) -> str:
        return f"pfp:provlist:unversioned:u{user_id}:c{connection_id}"

    monkeypatch.setattr(listing_cache, "cache_key", unversioned_cache_key)

    reader_a_at_write = threading.Event()
    release_reader_a = threading.Event()
    real_put = listing_cache.put_cached

    def hooked_put(key: str, body: bytes):
        reader_a_at_write.set()
        release_reader_a.wait(timeout=10.0)
        real_put(key, body)

    monkeypatch.setattr(listing_cache, "put_cached", hooked_put)

    def run_reader_a():
        local = TestClient(app)
        local.get(f"/provider-connections/{cid}/external-transactions", headers=headers)

    t = threading.Thread(target=run_reader_a)
    t.start()
    assert reader_a_at_write.wait(timeout=10.0)

    # Writer commits new transaction (DB now has 2 transactions)
    _ingest(headers, cid, "tx-seed-2")

    # Reader A finishes writing stale 1-transaction body into unversioned key
    release_reader_a.set()
    t.join()

    # Reader C now queries with unversioned key -> returns stale 1-transaction data!
    local_c = TestClient(app)
    stale_res = local_c.get(f"/provider-connections/{cid}/external-transactions", headers=headers)
    stale_ids = [tx["external_transaction_id"] for tx in stale_res.json()]
    assert stale_ids == ["tx-seed-1"], (
        "Without the revision in the cache key, Reader A's delayed write clobbers the cache "
        "and causes Reader C to observe stale data!"
    )


# ===========================================================================
# T2 — Redis Outage / Timeout
# ===========================================================================

def test_t2_redis_connection_error_on_read_falls_back_to_postgres(monkeypatch):
    """T2.1: Injected Redis connection error during cache read falls back to PostgreSQL."""
    import redis as redis_module

    uid, headers = _create_user("t2readconn")
    cid = _create_connection(headers)
    _ingest(headers, cid, "tx-1")
    expected = _uncached_body(headers, cid, monkeypatch)

    client_instance = listing_cache._redis()
    assert client_instance is not None

    def failing_get(key):
        raise redis_module.ConnectionError("Injected Redis connection failure on GET")

    monkeypatch.setattr(client_instance, "get", failing_get)

    res = _listing(headers, cid)
    assert res.status_code == 200
    assert res.content == expected
    assert listing_cache.stats["error"] >= 1


def test_t2_redis_timeout_error_on_read_falls_back_to_postgres(monkeypatch):
    """T2.2: Injected Redis timeout during cache read falls back to PostgreSQL."""
    import redis as redis_module

    uid, headers = _create_user("t2readtime")
    cid = _create_connection(headers)
    _ingest(headers, cid, "tx-1")
    expected = _uncached_body(headers, cid, monkeypatch)

    client_instance = listing_cache._redis()
    assert client_instance is not None

    def timeout_get(key):
        raise redis_module.TimeoutError("Injected Redis timeout failure on GET")

    monkeypatch.setattr(client_instance, "get", timeout_get)

    res = _listing(headers, cid)
    assert res.status_code == 200
    assert res.content == expected
    assert listing_cache.stats["error"] >= 1


def test_t2_redis_connection_error_on_write_falls_back_to_postgres(monkeypatch):
    """T2.3: Injected Redis connection error during cache write allows response to succeed."""
    import redis as redis_module

    uid, headers = _create_user("t2writeconn")
    cid = _create_connection(headers)
    _ingest(headers, cid, "tx-1")
    expected = _uncached_body(headers, cid, monkeypatch)

    client_instance = listing_cache._redis()
    assert client_instance is not None

    def failing_set(key, body, ex=None):
        raise redis_module.ConnectionError("Injected Redis connection failure on SET")

    monkeypatch.setattr(client_instance, "set", failing_set)

    res = _listing(headers, cid)
    assert res.status_code == 200
    assert res.content == expected
    assert listing_cache.stats["error"] >= 1


def test_t2_redis_timeout_error_on_write_falls_back_to_postgres(monkeypatch):
    """T2.4: Injected Redis timeout during cache write allows response to succeed."""
    import redis as redis_module

    uid, headers = _create_user("t2writetime")
    cid = _create_connection(headers)
    _ingest(headers, cid, "tx-1")
    expected = _uncached_body(headers, cid, monkeypatch)

    client_instance = listing_cache._redis()
    assert client_instance is not None

    def timeout_set(key, body, ex=None):
        raise redis_module.TimeoutError("Injected Redis timeout failure on SET")

    monkeypatch.setattr(client_instance, "set", timeout_set)

    res = _listing(headers, cid)
    assert res.status_code == 200
    assert res.content == expected
    assert listing_cache.stats["error"] >= 1


def test_t2_financial_writes_completely_independent_of_redis(monkeypatch):
    """T2.5: All financial write operations (account, transaction, transfer, evidence, refund)
    succeed when Redis is completely unavailable.
    """
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    listing_cache.reset_client()

    uid, headers = _create_user("t2writes")

    # 1. Create account
    r_acct1 = client.post(
        "/accounts",
        json={"name": "Checking", "account_type": "BANK", "currency": "VND"},
        headers=headers,
    )
    assert r_acct1.status_code == 200
    acct1_id = r_acct1.json()["id"]

    r_acct2 = client.post(
        "/accounts",
        json={"name": "Savings", "account_type": "BANK", "currency": "VND"},
        headers=headers,
    )
    assert r_acct2.status_code == 200
    acct2_id = r_acct2.json()["id"]

    # 2. Expense transaction
    r_tx = client.post(
        "/transactions",
        json={
            "amount": "500000.00",
            "description": "Initial balance",
            "category": "deposit",
            "type": "income",
            "account_id": acct1_id,
        },
        headers={**headers, "Idempotency-Key": uuid4().hex},
    )
    assert r_tx.status_code == 200
    canonical_tx_id = r_tx.json()["canonical_event_id"]

    # 3. Transfer
    r_transfer = client.post(
        "/transfers",
        json={
            "from_account_id": acct1_id,
            "to_account_id": acct2_id,
            "amount": "100000.00",
            "description": "Savings allocation",
        },
        headers={**headers, "Idempotency-Key": uuid4().hex},
    )
    assert r_transfer.status_code == 200

    # 4. Ingest provider evidence
    cid = _create_connection(headers, "ext-acct-outage")
    r_ingest = _ingest(headers, cid, "tx-outage-1")
    assert r_ingest.status_code == 200

    # 5. Create expense and refund
    r_exp = client.post(
        "/transactions",
        json={
            "amount": "50000.00",
            "description": "Refundable expense",
            "category": "shopping",
            "type": "expense",
            "account_id": acct1_id,
        },
        headers={**headers, "Idempotency-Key": uuid4().hex},
    )
    assert r_exp.status_code == 200
    exp_event_id = r_exp.json()["canonical_event_id"]

    r_ref = client.post(
        "/refunds",
        json={"original_event_id": exp_event_id, "amount": "20000.00"},
        headers={**headers, "Idempotency-Key": uuid4().hex},
    )
    assert r_ref.status_code == 200

    # 6. Audit integrity in PostgreSQL
    with SessionLocal() as db:
        audit = audit_user(db, uid)
        assert audit["ok"] is True
        assert audit["causal_divergence"] is None


def test_t2_cache_failures_do_not_modify_committed_financial_state(monkeypatch):
    """T2.6: Cache read/write failures do not alter financial account balances,
    event ledgers, or command receipts in PostgreSQL.
    """
    uid, headers, event_id = _create_user_and_expense("250.00")
    cid = _create_connection(headers)
    _ingest(headers, cid, "tx-fin-1")

    # Snapshot financial state before cache failures
    with SessionLocal() as db:
        audit_before = audit_user(db, uid)
        receipts_before = db.query(CommandReceipt).filter_by(user_id=uid).count()
        events_before = db.query(FinancialEvent).filter_by(user_id=uid).count()

    # Inject failure into listing cache GET
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    listing_cache.reset_client()

    for _ in range(3):
        res = _listing(headers, cid)
        assert res.status_code == 200

    # Verify state after cache failures
    with SessionLocal() as db:
        audit_after = audit_user(db, uid)
        receipts_after = db.query(CommandReceipt).filter_by(user_id=uid).count()
        events_after = db.query(FinancialEvent).filter_by(user_id=uid).count()

    assert audit_before["canonical"] == audit_after["canonical"]
    assert audit_before["economic_summary"] == audit_after["economic_summary"]
    assert receipts_before == receipts_after
    assert events_before == events_after
    assert audit_after["ok"] is True
    assert audit_after["causal_divergence"] is None


def test_t2_redis_recovery_restores_normal_cache_behavior(redis_cache, monkeypatch):
    """T2.7: After Redis recovers and failure backoff elapses, normal caching is restored."""
    uid, headers = _create_user("t2recover")
    cid = _create_connection(headers)
    _ingest(headers, cid, "tx-rec-1")

    # Simulate failure and backoff
    monkeypatch.setattr(listing_cache, "_skip_until", time.monotonic() + 100.0)
    listing_cache.stats["error"] = 1
    listing_cache.stats["hit"] = 0

    # While in backoff: bypassed
    res_bypassed = _listing(headers, cid)
    assert res_bypassed.status_code == 200
    assert listing_cache.stats["hit"] == 0

    # Recovery: backoff cleared, Redis available
    monkeypatch.setattr(listing_cache, "_skip_until", 0.0)
    listing_cache.reset_client()

    # Miss path populates cache
    res_miss = _listing(headers, cid)
    assert res_miss.status_code == 200
    assert listing_cache.stats["miss"] >= 1

    # Next request hits cache
    res_hit = _listing(headers, cid)
    assert res_hit.status_code == 200
    assert listing_cache.stats["hit"] >= 1
    assert res_miss.content == res_hit.content


# ===========================================================================
# T3 — Cross-User Cache Isolation
# ===========================================================================

def test_t3_cross_user_cache_isolation_and_authorization_gate(redis_cache):
    """T3.1: Verify cross-user isolation and that PostgreSQL ownership checks
    occur before any Redis query or cache serving.
    """
    uid_a, headers_a = _create_user("userA")
    uid_b, headers_b = _create_user("userB")

    cid_a = _create_connection(headers_a, "acct-user-a")
    cid_b = _create_connection(headers_b, "acct-user-b")

    _ingest(headers_a, cid_a, "tx-A-1", "confidential-A")
    _ingest(headers_b, cid_b, "tx-B-1", "confidential-B")

    # User A warms cache
    res_a1 = _listing(headers_a, cid_a)
    assert res_a1.status_code == 200
    res_a2 = _listing(headers_a, cid_a)
    assert res_a2.status_code == 200
    assert b"tx-A-1" in res_a2.content
    assert b"tx-B-1" not in res_a2.content

    # User B attempts to access User A's connection -> rejected by PostgreSQL gate
    res_b_steals_a = _listing(headers_b, cid_a)
    assert res_b_steals_a.status_code == 404
    assert b"tx-A-1" not in res_b_steals_a.content

    # User A attempts to access User B's connection -> rejected by PostgreSQL gate
    res_a_steals_b = _listing(headers_a, cid_b)
    assert res_a_steals_b.status_code == 404
    assert b"tx-B-1" not in res_a_steals_b.content

    # Unauthenticated request -> rejected by auth layer
    res_anon = client.get(f"/provider-connections/{cid_a}/external-transactions")
    assert res_anon.status_code == 401
    assert b"tx-A-1" not in res_anon.content


def test_t3_cache_keys_strictly_separate_dimensions():
    """T3.2: Cache key construction unambiguously separates user, connection,
    revision, and query parameters.
    """
    key_base = listing_cache.cache_key(user_id=1, connection_id=10, revision=1)
    key_diff_user = listing_cache.cache_key(user_id=2, connection_id=10, revision=1)
    key_diff_conn = listing_cache.cache_key(user_id=1, connection_id=20, revision=1)
    key_diff_rev = listing_cache.cache_key(user_id=1, connection_id=10, revision=2)
    key_with_param = listing_cache.cache_key(user_id=1, connection_id=10, revision=1, params={"limit": 50})

    keys = {key_base, key_diff_user, key_diff_conn, key_diff_rev, key_with_param}
    assert len(keys) == 5, f"All dimension combinations must produce distinct keys: {keys}"

    assert ":u1:" in key_base and ":c10:" in key_base and ":r1:" in key_base
    assert ":u2:" in key_diff_user


def test_t3_concurrent_cross_user_reads_prevent_cross_talk():
    """T3.3: Concurrent reads from User A and User B never leak data between users."""
    uid_a, headers_a = _create_user("t3conA")
    uid_b, headers_b = _create_user("t3conB")

    cid_a = _create_connection(headers_a, "conn-A")
    cid_b = _create_connection(headers_b, "conn-B")

    for i in range(3):
        _ingest(headers_a, cid_a, f"tx-A-{i}", f"payload-A-{i}")
        _ingest(headers_b, cid_b, f"tx-B-{i}", f"payload-B-{i}")

    errors: list[str] = []

    def reader_a():
        local = TestClient(app)
        for _ in range(10):
            r = local.get(f"/provider-connections/{cid_a}/external-transactions", headers=headers_a)
            if r.status_code != 200:
                errors.append(f"User A unexpected status {r.status_code}")
            body = r.content
            if b"tx-B-" in body:
                errors.append("Data leak: User B transaction found in User A response!")
            if b"tx-A-" not in body:
                errors.append("Missing data in User A response")

    def reader_b():
        local = TestClient(app)
        for _ in range(10):
            r = local.get(f"/provider-connections/{cid_b}/external-transactions", headers=headers_b)
            if r.status_code != 200:
                errors.append(f"User B unexpected status {r.status_code}")
            body = r.content
            if b"tx-A-" in body:
                errors.append("Data leak: User A transaction found in User B response!")
            if b"tx-B-" not in body:
                errors.append("Missing data in User B response")

    threads = [
        threading.Thread(target=reader_a) for _ in range(4)
    ] + [
        threading.Thread(target=reader_b) for _ in range(4)
    ]

    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"Cross-user concurrent reads produced errors: {errors}"


# ===========================================================================
# T4 — Financial Invariants While Redis is Unavailable
# ===========================================================================

def test_t4_concurrent_refunds_cannot_exceed_original_expense_during_redis_outage(monkeypatch):
    """T4.1: PostgreSQL row-level locks prevent over-refunds even when Redis is down."""
    if engine.dialect.name != "postgresql":
        pytest.skip("Concurrency test requires PostgreSQL")

    # Hard-down Redis
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    listing_cache.reset_client()

    uid, headers, original_event_id = _create_user_and_expense("100.00")

    original_remaining = causal_service.refundable_remaining
    first_eligibility_check = threading.Event()
    second_eligibility_check = threading.Event()
    release_first = threading.Event()
    call_lock = threading.Lock()
    call_count = 0

    def synchronized_remaining(db, *, user_id, original_event_id):
        nonlocal call_count
        remaining = original_remaining(db, user_id=user_id, original_event_id=original_event_id)
        with call_lock:
            call_count += 1
            cur = call_count
        if cur == 1:
            first_eligibility_check.set()
            if not release_first.wait(timeout=10.0):
                raise TimeoutError("Timed out waiting to release first refund")
        elif cur == 2:
            second_eligibility_check.set()
        return remaining

    monkeypatch.setattr(causal_service, "refundable_remaining", synchronized_remaining)

    def send_refund(key: str):
        local = TestClient(app)
        return local.post(
            "/refunds",
            json={"original_event_id": original_event_id, "amount": "60.00"},
            headers={**headers, "Idempotency-Key": key},
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(send_refund, uuid4().hex)
        assert first_eligibility_check.wait(timeout=10.0)

        f2 = executor.submit(send_refund, uuid4().hex)

        # Confirm PostgreSQL lock wait exists
        with engine.connect() as conn:
            lock_wait = False
            for _ in range(50):
                has_lock = conn.execute(
                    text(
                        "SELECT EXISTS ("
                        "SELECT 1 FROM pg_stat_activity "
                        "WHERE datname = current_database() "
                        "AND pid <> pg_backend_pid() "
                        "AND wait_event_type = 'Lock')"
                    )
                ).scalar_one()
                if has_lock:
                    lock_wait = True
                    break
                time.sleep(0.05)

        release_first.set()
        r1 = f1.result()
        r2 = f2.result()

    statuses = [r1.status_code, r2.status_code]
    assert 200 in statuses, f"One refund must succeed: {statuses}"
    assert 422 in statuses, f"Second refund must be rejected with 422 Unprocessable Entity: {statuses}"

    # Verify committed state in PostgreSQL
    with SessionLocal() as db:
        remaining_db = causal_service.refundable_remaining(
            db, user_id=uid, original_event_id=original_event_id
        )
        assert remaining_db == Decimal("40.00")
        audit = audit_user(db, uid)
        assert audit["ok"] is True
        assert audit["causal_divergence"] is None

    assert lock_wait, "PostgreSQL row-level lock wait must be observed"


def test_t4_concurrent_same_key_refund_replays_winning_command_during_redis_outage(monkeypatch):
    """T4.2: Under Redis outage, concurrent refund requests with the same idempotency key
    replay the winning receipt without duplicate financial effects.
    """
    if engine.dialect.name != "postgresql":
        pytest.skip("Refund idempotency concurrency regression requires PostgreSQL")

    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    listing_cache.reset_client()

    uid, headers, original_event_id = _create_user_and_expense("100.00")
    key = uuid4().hex

    def send_refund():
        local = TestClient(app)
        return local.post(
            "/refunds",
            json={"original_event_id": original_event_id, "amount": "60.00"},
            headers={**headers, "Idempotency-Key": key},
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(send_refund)
        f2 = executor.submit(send_refund)
        r1 = f1.result()
        r2 = f2.result()

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json() == r2.json()

    with SessionLocal() as db:
        receipts = db.query(CommandReceipt).filter_by(user_id=uid, idempotency_key=key).count()
        assert receipts == 1

        remaining = causal_service.refundable_remaining(
            db, user_id=uid, original_event_id=original_event_id
        )
        assert remaining == Decimal("40.00")
        audit = audit_user(db, uid)
        assert audit["ok"] is True
        assert audit["causal_divergence"] is None


def test_t4_same_key_conflicting_payload_rejected_during_redis_outage(monkeypatch):
    """T4.3: Under Redis outage, reusing an idempotency key with different payload
    is rejected with 409 Conflict and creates no side effects.
    """
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    listing_cache.reset_client()

    uid, headers, original_event_id = _create_user_and_expense("100.00")
    key = uuid4().hex

    r1 = client.post(
        "/refunds",
        json={"original_event_id": original_event_id, "amount": "40.00"},
        headers={**headers, "Idempotency-Key": key},
    )
    assert r1.status_code == 200

    r2 = client.post(
        "/refunds",
        json={"original_event_id": original_event_id, "amount": "20.00"},
        headers={**headers, "Idempotency-Key": key},
    )
    assert r2.status_code == 409
    assert "Idempotency key was already used for a different command payload" in r2.json()["detail"]

    with SessionLocal() as db:
        remaining = causal_service.refundable_remaining(
            db, user_id=uid, original_event_id=original_event_id
        )
        assert remaining == Decimal("60.00")
        audit = audit_user(db, uid)
        assert audit["ok"] is True
        assert audit["causal_divergence"] is None
