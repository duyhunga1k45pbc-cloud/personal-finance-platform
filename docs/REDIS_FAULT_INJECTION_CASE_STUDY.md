# Redis Fault Injection and Financial Invariant Verification Case Study

This case study documents the verification and fault-injection testing of the Personal Finance Platform (PFP) Redis caching subsystem and its boundary with PostgreSQL financial domain invariants.

Testing was conducted using **real PostgreSQL** and **real Redis** instances with deterministic synchronization barriers rather than heuristic sleeps.

---

## Executive Summary of Results

| Scenario | Scope | Result | Verification Method |
|---|---|---|---|
| **T1** | Deterministic Stale Cache Fill Race | **PASSED** | Thread synchronization events pausing Reader A at Redis write boundary; Writer B commits and bumps revision via PostgreSQL trigger; Reader C reads new revision |
| **T1 Regression** | Missing Revision Dimension | **REPRODUCED** | Counterfactual test omitting revision from cache key; slow Reader A clobbers cache, forcing Reader C to observe stale data |
| **T2** | Redis Outage & Timeout Resiliency | **PASSED** | Injected connection errors and timeouts at read and write boundaries; verified fallback, backoff, and full recovery |
| **T2 Independence**| Financial Write Decoupling | **PASSED** | Accounts, transactions, transfers, provider sync, and refunds executed during total Redis outage; ledger verified intact |
| **T3** | Cross-User Cache Isolation | **PASSED** | PostgreSQL ownership gate checked before Redis; cross-tenant GET returns 404; concurrent multi-threaded reads show 0 data leakage |
| **T4** | Financial Invariants During Outage | **PASSED** | Concurrent racing refunds under Redis outage; PostgreSQL `SELECT FOR UPDATE` serialized requests; over-refund prevented (60.00 VND <= 100.00 VND) |
| **T4 Idempotency** | Same-Key Replay & Conflict | **PASSED** | Concurrent same-key requests replay winning command receipt; conflicting payloads rejected with 409 Conflict |

---

## Detailed Scenario Analysis

### T1 — Deterministic Stale Cache Fill Race

#### 1. Invariant
A reader that fetches an older database listing snapshot and stalls mid-flight must never clobber the cache or cause subsequent readers to observe stale data if a writer committed new records in the interim.

#### 2. Failure Hypothesis
In traditional cache-aside designs where cache keys lack committed revision metadata (e.g., `pfp:provlist:u{uid}:c{cid}`), the following race condition causes stale cache pollution:
1. Reader A reads database state at revision $r_0$ (containing $N$ items) and prepares cache payload.
2. Reader A is delayed (GC pause, network latency, thread preemption) before writing to Redis.
3. Writer B commits new transactions in PostgreSQL, advancing state to $r_1$ (containing $N+1$ items).
4. Reader A resumes and writes its stale $N$-item payload to Redis.
5. Reader C executes after Writer B has committed, consults Redis, and serves the stale $N$-item payload, violating read-after-write consistency.

#### 3. Fault Injection Method
- **Seam:** Wrapped `listing_cache.put_cached` with threading `Event` barriers (`reader_a_at_write_boundary` and `release_reader_a`).
- **Execution:**
  1. Seeded provider transactions and recorded initial committed revision $r_0$.
  2. Reader A issued `GET /provider-connections/{cid}/external-transactions`, read revision $r_0$, loaded PostgreSQL data, and paused at the `put_cached` entry boundary.
  3. While Reader A was paused, a separate writer committed an additional transaction to PostgreSQL.
  4. PostgreSQL statement-level trigger `trg_external_transactions_listing_revision` atomically updated `provider_listing_revisions.revision` to $r_1 > r_0$ inside the writer's transaction.
  5. Reader A was released and completed writing to Redis under key `pfp:provlist:s1:u{uid}:c{cid}:r{r0}:p{hash}`.
  6. Reader C initiated a request strictly after the writer commit.

#### 4. Observed Behavior
- Reader A completed with HTTP 200 containing the initial 2 transactions.
- Reader C executed `read_revision(db, cid)`, retrieved $r_1$, computed cache key `...:r{r1}:...`, experienced a cache miss, loaded all 3 transactions from PostgreSQL, cached them under $r_1$, and returned HTTP 200.
- Direct inspection of Redis confirmed both keys existed independently: the $r_0$ key held the 2-item payload, and the $r_1$ key held the 3-item payload. Reader C was completely immune to Reader A's delayed write.

#### 5. Root Cause & Architectural Fix
- **Root Cause:** Asynchronous cache updates decoupled from transactional commit timestamps create write-race windows.
- **Architectural Fix:** Dedicated PostgreSQL table `provider_listing_revisions` updated via statement triggers `trg_external_transactions_listing_revision` and `trg_external_transaction_evidence_listing_revision`. The committed revision is an immutable component of the Redis cache key.
- **Counterfactual Regression Test:** In `test_t1_regression_missing_revision_causes_stale_cache_fill`, monkeypatching the cache key to omit the revision allowed Reader A's delayed write to overwrite the cache, causing Reader C to return stale data (`["tx-seed-1"]` instead of `["tx-seed-1", "tx-seed-2"]`).

#### 6. Limitations
- Revision advancement is per provider connection. Frequent writes to the same connection will invalidate read caches rapidly and cause old revision keys to linger in Redis until their TTL expires (60s default).

---

### T2 — Redis Outage / Timeout Resiliency

#### 1. Invariant
Redis is strictly an acceleration layer. Redis connection drops, request timeouts, and server crashes must never:
1. Prevent authorized clients from retrieving listing data (must fall back to PostgreSQL).
2. Interfere with or prevent financial write operations.
3. Mutate or corrupt committed financial ledger state.

#### 2. Failure Hypothesis
Unhandled network errors (e.g. `ConnectionError`, `TimeoutError`) or hard outages could cause unhandled HTTP 500 responses on read endpoints, block write endpoints if Redis operations were coupled to the write path, or leave database transactions in ambiguous states.

#### 3. Fault Injection Method
- **Read Boundary:** Injected `redis.exceptions.ConnectionError` and `redis.exceptions.TimeoutError` on `client.get()`.
- **Write Boundary:** Injected `redis.exceptions.ConnectionError` and `redis.exceptions.TimeoutError` on `client.set()`.
- **Total Outage:** Reconfigured `REDIS_URL` to point to an unreachable endpoint (`redis://127.0.0.1:1/0`).
- **Financial Write Exerciser:** Executed account creation, income/expense logging, internal transfers, provider evidence ingestion, and refunds while Redis was completely down.
- **Audit:** Compared `audit_user(db, uid)` before and after simulated cache failures.

#### 4. Observed Behavior
- **Graceful Fallback:** All read requests during read/write errors returned HTTP 200 with content identical to the uncached database response.
- **Failure Backoff:** Cache failures incremented `stats["error"]` and activated a 5-second backoff window (`_skip_until`), allowing subsequent requests to bypass Redis immediately without incurring connection timeout penalties.
- **Write Independence:** Every financial write completed with HTTP 200. Canonical financial event records and command receipts were committed to PostgreSQL without touching Redis.
- **Zero Ledger Corruption:** Pre- and post-failure audits confirmed identical account balances, transaction counts, command receipts, and zero causal divergence (`audit["ok"] is True`).
- **Self-Healing Recovery:** Resetting the backoff window and restoring Redis connectivity allowed the cache to seamlessly resume serving cache hits.

#### 5. Root Cause & Architectural Fix
- **Root Cause:** Tight coupling to caching infrastructure or unhandled network exceptions in middleware/handlers.
- **Architectural Fix:** Enclosed Redis calls in defensive try/except blocks in `backend/app/listing_cache.py`, with failure recording and exponential/monotonic backoff (`_skip_until`). Ensured write endpoints never depend on Redis.

#### 6. Limitations
- During a sustained Redis outage, read traffic falls back directly to PostgreSQL, which increases database load and could cause database connection pool contention under high concurrency.

---

### T3 — Cross-User Cache Isolation

#### 1. Invariant
A user must never access or view another user's provider connection transactions, regardless of whether requests result in cache misses, cache hits, or occur under concurrent multi-threaded execution.

#### 2. Failure Hypothesis
If cache checks were performed prior to PostgreSQL authorization or if cache keys lacked tenant isolation (e.g., omitting `user_id` or `connection_id`), User B could craft or hit User A's cached entries.

#### 3. Fault Injection Method
- Created User A and User B with distinct credentials and provider connections (Connection A and Connection B).
- Ingested confidential transaction payloads into Connection A and Connection B.
- Warmed cache entries for both users.
- Attempted cross-tenant access: User B requested Connection A (`GET /provider-connections/{cid_a}/external-transactions`) with User B's authentication header.
- Fired 8 concurrent threads (4 reading User A, 4 reading User B) performing 80 simultaneous requests.

#### 4. Observed Behavior
- Cross-tenant requests were blocked with HTTP 404 by `get_owned_provider_connection(db, user_id, connection_id)` in PostgreSQL before Redis was consulted.
- Unauthenticated requests were blocked with HTTP 401.
- Cache keys explicitly segregated tenants: `...:u{user_id}:c{connection_id}:r{revision}:p{params}`.
- Concurrent reader threads reported 0 errors, 0 cross-talk, and 0 data leakage.

#### 5. Architectural Guardrail
- Database-backed ownership verification in PostgreSQL is executed as the primary dependency on every request before entering the cache subsystem.

#### 6. Limitations
- Does not protect against an attacker with physical or network root access to the Redis instance unless payload encryption or message authentication codes (HMAC) are applied to cached values.

---

### T4 — Financial Invariants While Redis is Unavailable

#### 1. Invariant
Financial correctness—specifically over-refund prevention, transactional idempotency replay, conflicting payload rejection, and double-entry ledger balance—must remain invariant regardless of Redis availability.

#### 2. Failure Hypothesis
If distributed locking, idempotency tracking, or financial state depended on Redis, a Redis failure could lead to race conditions, over-refunds exceeding the original expense, or duplicate financial disbursements.

#### 3. Fault Injection Method
- Completely disabled Redis (`REDIS_URL=redis://127.0.0.1:1/0`).
- **Concurrent Over-Refund Test:** Created a 100.00 VND expense. Injected synchronization on `causal_service.refundable_remaining` to release two 60.00 VND refund requests simultaneously across two worker threads.
- Monitored PostgreSQL `pg_stat_activity` for row-level lock waits (`wait_event_type = 'Lock'`).
- **Idempotency Replay Test:** Submitted identical 60.00 VND refund requests concurrently with the same idempotency key during the outage.
- **Conflicting Payload Test:** Submitted an initial 40.00 VND refund, followed by a request reusing the same idempotency key with a 20.00 VND amount.

#### 4. Observed Behavior
- **Locking & Over-Refund Prevention:**
  - One refund thread acquired the row lock via `SELECT ... FOR UPDATE` in PostgreSQL, checked remaining capacity (100.00 VND), and committed a 60.00 VND refund (HTTP 200).
  - The second refund thread waited on the PostgreSQL row lock. Upon waking, it re-evaluated remaining capacity (40.00 VND < 60.00 VND) and was rejected with HTTP 422.
  - The aggregate refunded amount was exactly 60.00 VND <= 100.00 VND.
- **Idempotency Replay:** Both concurrent requests with identical keys succeeded (HTTP 200) and returned the exact same financial event ID and receipt. Exactly one financial event was created.
- **Conflicting Payload:** The conflicting request was rejected with HTTP 409 (`Idempotency key was already used for a different command payload`) without mutating financial state.
- **Audit Verification:** `audit_user` confirmed complete causal and economic consistency (`causal_divergence: None`, `ok: True`).

#### 5. Architectural Foundation
- Financial write paths use PostgreSQL ACID transactions with row-level locking (`SELECT ... FOR UPDATE`), append-only financial event ledgers, and database-backed `CommandReceipt` records. Redis is entirely absent from the financial write path.

---

## Verification Evidence

### Test Suite Execution
- **Focused Test Suite:** `backend/tests/test_redis_fault_injection_correctness.py`
  - Command: `DATABASE_URL=postgresql://postgres:postgres@localhost:5432/finance_test_db REDIS_TEST_URL=redis://localhost:6379/15 REQUIRE_REDIS_TESTS=1 pytest tests/test_redis_fault_injection_correctness.py -v`
  - Result: **15 passed, 0 failed, 0 skipped** (8.28s)

- **Related Integration Test Suites:**
  - `tests/test_provider_listing_cache.py` (17 passed)
  - `tests/test_provider_listing_revision_concurrency.py` (4 passed)
  - `tests/test_refund_concurrency.py` (3 passed)
  - Result: **24 passed, 0 failed, 0 skipped** (13.50s)

- **Full Repository Regression Suite:**
  - Command: `DATABASE_URL=postgresql://postgres:postgres@localhost:5432/finance_test_db REDIS_TEST_URL=redis://localhost:6379/15 REQUIRE_REDIS_TESTS=1 pytest`
  - Result: **328 passed, 0 failed, 0 skipped** (142.25s)

---

## Architectural Conclusions

1. **Transactional Revisioning Eliminates Cache Invalidation Races:** Combining PostgreSQL statement triggers with revisioned cache keys prevents stale cache fill races without requiring distributed coordination or distributed transactions.
2. **PostgreSQL Authorization Must Precede Cache Access:** Authenticating and authorizing requests against the canonical database on every request prevents cross-tenant data leakage through the cache.
3. **Decoupling Financial Writes Protects Core Invariants:** Treating Redis exclusively as an optional read-side optimization ensures financial invariants (over-refund prevention, idempotency, and auditability) remain completely resilient to cache failures.
