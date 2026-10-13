# Personal Finance Platform

**A correctness-focused financial backend engineering case study built with FastAPI, PostgreSQL, and SQLAlchemy.**

This project explores how financial state can remain consistent and recoverable when requests retry, concurrent operations conflict, transactions fail, external evidence disagrees, or execution conditions change.

The backend implements financial event modeling, transaction and concurrency controls, idempotent commands, reconciliation, recovery tooling, authorization, and optional Redis caching. The repository includes PostgreSQL-backed integration tests, failure-injection scenarios, and performance measurements for selected operations.

**Project status:** Engineering portfolio and experimental application. Not production-deployed.

**Quick navigation:** [Over-refund](https://github.com/duyhunga1k45pbc-cloud/personal-finance-platform#case-study-preventing-concurrent-over-refunds) · [Redis revision cache](https://github.com/duyhunga1k45pbc-cloud/personal-finance-platform#case-study-transaction-consistent-redis-caching) · [Redis fault injection](https://github.com/duyhunga1k45pbc-cloud/personal-finance-platform#case-study-redis-fault-injection-and-invariant-verification) · [Connection pool RCA](https://github.com/duyhunga1k45pbc-cloud/personal-finance-platform#case-study-connection-pool-starvation) · [Index & pagination](https://github.com/duyhunga1k45pbc-cloud/personal-finance-platform#case-study-deterministic-pagination-and-postgresql-indexing) · [Moneta demo](https://github.com/duyhunga1k45pbc-cloud/personal-finance-platform#moneta-demo-ui) · [Test results](https://github.com/duyhunga1k45pbc-cloud/personal-finance-platform#test-results-and-coverage)

## Engineering Highlights

| **Area**                  | **Implemented and verified behavior**                                                                                                     |
| ------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| Financial domain modeling | Canonical financial events, signed entries, transfers, refunds, reversals, and corrections                                                |
| Concurrency control       | PostgreSQL row locking, expected-version guards, deterministic conflicting transactions                                                   |
| Transactional idempotency | Database-backed command receipts, retry recovery, concurrent same-key replay                                                              |
| Historical integrity      | Append-only financial history and causal event relationships                                                                              |
| Reconciliation            | Explicit handling of conflicting and incomplete external evidence                                                                         |
| Recovery                  | Rebuildable projections, canonical-state audits, backup/restore exercises                                                                 |
| Failure injection         | Commit acknowledgment loss, worker termination, deadlocks, connection-pool exhaustion, Redis failures and deterministic stale-cache races |
| Security                  | JWT authentication, declared route policies, SQL ownership scoping, short-lived auth sessions                                             |
| Schema evolution          | Alembic migrations, rollback verification, historical backfill, revision compatibility checks                                             |
| Async correctness         | SQLAlchemy AsyncSession, cancellation, connection lifecycle, native-async experiments                                                     |
| Cache correctness         | PostgreSQL-revisioned Redis keys, ownership-before-cache checks, 15 reported fault-injection tests including a vulnerable counterfactual  |
| Read performance          | N+1 removal, PostgreSQL-revisioned Redis cache, query-plan analysis and controlled benchmarks                                             |
| Query correctness         | Deterministic transaction pagination and a measured composite-index optimization (merged to `main` in PR #6 on 2026-10-12)                |
| Demo                      | Moneta read-only web interface over existing authenticated FastAPI APIs                                                                   |

**Latest reported local regression:** **328 passed, 0 failed, 0 skipped**, using PostgreSQL and real Redis integration tests. This includes **15 newly reported Redis fault-injection tests** in `backend/tests/test_redis_fault_injection_correctness.py`. These are agent-reported local execution results, not independent verification or a claim about the latest GitHub Actions run; see [Test Results and Coverage](https://github.com/duyhunga1k45pbc-cloud/personal-finance-platform#test-results-and-coverage).

The verification applies to the scenarios exercised by the tests. It does not establish exhaustive correctness or production readiness.

---

## Case Study: Preventing Concurrent Over-Refunds

One of the project's most important findings was a reproducible concurrency defect in the refund workflow.

### The Business Invariant

For an original expense, the total amount of active refunds must not exceed the original refundable amount.

For example:

- Original expense: 100
- Refund request A: 60
- Refund request B: 60

Both refunds must not succeed.

### The Failure

Before the concurrency fix, two independent PostgreSQL transactions could execute the following sequence:

1. Transaction A reads the remaining refundable balance as 100.
2. Transaction B independently reads the same remaining balance as 100.
3. Both transactions validate their refund amounts against the stale balance.
4. Both create their own refund events and causal links.
5. Both commit successfully.

The resulting active refund total becomes **120 against an original expense of 100**.

Each individual transaction is atomic, but the aggregate business invariant is violated.

This demonstrates that transaction atomicity and idempotency do not, by themselves, guarantee correctness across concurrent operations.

### Root Cause

Refund eligibility was evaluated without first serializing commands against their shared original financial event.

The application already implemented the business invariant for sequential execution. The missing guarantee was concurrency protection around the eligibility check and subsequent write.

### The Fix

The refund and reversal paths now acquire a PostgreSQL `SELECT FOR UPDATE` row lock on the original financial event before evaluating eligibility.

The lock remains held until the transaction commits or rolls back.

Related correction and void operations follow the same original-event locking convention before evaluating causal dependencies.

Under the verified PostgreSQL `READ COMMITTED` isolation level, a competing transaction waits for the lock and then reevaluates eligibility against the committed state.

### Preserving Idempotency

The serialization change also exposed an interaction with concurrent idempotent retries.

Two requests using the same command identity may compete for the original-event lock.

After the winning request commits, the waiting request must recover the already committed command result rather than fail merely because the refundable balance has changed.

The implementation includes receipt rechecking after causal rejection and rollback to preserve the same-key replay contract.

### Verification Evidence

A deterministic PostgreSQL regression test was added to reproduce the original failure and verify the corrected behavior.

The tests confirm:

- The vulnerable implementation permitted two successful 60-unit refunds against an expense of 100.
- The corrected implementation prevents both conflicting refunds from committing.
- PostgreSQL lock contention is observed.
- The persisted refund aggregate remains within the original amount.
- Concurrent same-key retries recover the winning response without duplicating financial effects.
- Competing refund and reversal commands respect the shared serialization boundary.
- Relevant canonical records, causal links, historical records, and audit state remain consistent in the tested scenarios.

The original fix was verified using a targeted PostgreSQL regression suite; current reported regression results appear in [Test Results and Coverage](https://github.com/duyhunga1k45pbc-cloud/personal-finance-platform#test-results-and-coverage).

**Implementation references:**

- `backend/app/causal_service.py`
- `backend/app/canonical_service.py`
- `backend/app/routers/causal_events.py`
- `backend/tests/test_refund_concurrency.py`
- `backend/tests/test_async_sqlalchemy_correctness.py`

This case study documents a verified failure, its root cause, an implemented correction, and regression evidence. It does not claim that all possible concurrency defects have been eliminated.

---

## Case Study: Transaction-Consistent Redis Caching

A provider external-transaction listing had measurable read-side costs. Removing an N+1 pattern reduced the query count, and a Redis prototype accelerated high-hit workloads. But a financial-data cache is useful only if it does not silently serve an outdated version after a committed write.

### The Failure Boundary

An invalidation sequence such as `COMMIT` followed by `Redis DEL` contains a crash window: the database commit can succeed while the cache invalidation never happens. A TTL bounds how long the error persists; it does **not** establish read consistency.

### The Design

Each provider connection has a **listing revision stored in PostgreSQL**. Database triggers advance it in the **same transaction** as inserts into provider transactions or evidence. A rollback rolls back the revision as well.

```
Provider writer transaction
  +-- Insert transaction / evidence
  +-- Advance PostgreSQL listing revision
  +-- COMMIT (or roll back both)
                       |
                       v
Authorized listing GET
  +-- Verify user and connection ownership in PostgreSQL
  +-- Read committed listing revision
  +-- Redis key: user + connection + query + revision
       | HIT                       | MISS
       v                           v
  Return cached body      Query PostgreSQL and populate Redis

```

An in-flight reader can repopulate an older revision key, but subsequent requests look up the newer revision. Redis outages fall back to PostgreSQL; **ownership verification takes place before Redis access**. The cache is optional and disabled without `REDIS_URL`.

### Verification and Trade-offs

PostgreSQL and Redis integration tests cover rollback, savepoints, concurrent writers, cross-user isolation, stale in-flight cache fills, malformed cache data, and Redis failure. A controlled local experiment (20 users, 300 transactions per connection, 6 concurrent clients) measured **144 → 271 RPS** for read-only traffic and **126 → 198 RPS** for mixed read/write traffic. These synthetic results depend on cache hit rate and are not production SLOs.

The design still makes PostgreSQL authorization/revision queries on a cache hit, serializes revision updates for the same connection, and retains older Redis keys until their TTLs expire. The provider listing still needs pagination for very large histories.

**Implementation references:** `backend/app/listing_cache.py`, migration `k1b9c6d3f477`, `backend/tests/test_provider_listing_cache.py`, and `backend/tests/test_provider_listing_revision_concurrency.py`. The separate [fault-injection case study](https://github.com/duyhunga1k45pbc-cloud/personal-finance-platform#case-study-redis-fault-injection-and-invariant-verification) evaluates the failure boundaries of this design.

---

## Case Study: Redis Fault Injection and Invariant Verification

The revisioned cache was designed to be optional: PostgreSQL remains the authoritative source for provider listings and financial writes, while Redis stores derived listing responses. The next question was whether **cache failures or concurrent requests could break freshness, ownership, or financial invariants** despite those boundaries.

### Invariants Under Test

- **Listing freshness:** A request beginning after a committed provider write must not select an older listing revision for that connection. An already-running request may still complete using the earlier snapshot it read.
- **Tenant isolation:** A cache hit must not bypass the PostgreSQL ownership check for a provider connection.
- **Financial correctness:** Redis unavailability must not permit duplicate financial effects or over-refunds; financial commands remain subject to PostgreSQL idempotency and locking.
- **Graceful degradation:** Cache-read and cache-write failures should fall back to PostgreSQL for the listing endpoints that promise that behavior.

### Controlled Failure Experiments

| **Scenario**                                    | **Injected condition**                                                                                                                   | **Reported outcome**                                                                                                                            |
| ----------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| **T1 — Stale cache fill**                       | Reader A loads revision `R`, pauses before its Redis write; another transaction commits revision `R+1`; Reader A then writes the old key | A later reader looks up `R+1` and receives the updated listing, rather than selecting `R`                                                       |
| **T1 — Counterfactual**                         | Repeat the vulnerable interleaving with a key that omits revision                                                                        | The deliberately unversioned cache demonstrates a stale-fill failure; this is **not** a defect observed in the current versioned implementation |
| **T2 — Redis outage and timeout**               | Raise connection/read/write errors; exercise the fallback and recovery paths                                                             | Listing responses fall back to PostgreSQL in the tested paths; errors and backoff are recorded                                                  |
| **T3 — Cross-user access**                      | Interleave requests from distinct users and attempt another user's connection ID                                                         | PostgreSQL ownership validation rejects the request before consulting Redis                                                                     |
| **T4 — Concurrent refunds during Redis outage** | Run conflicting refunds and same-key retries while Redis is unavailable                                                                  | Database locking and command receipts preserve the tested refund and idempotency invariants                                                     |

T1 uses synchronization events to control the interleaving rather than relying on arbitrary sleeps. The counterfactual experiment is especially useful: it demonstrates **why the revision dimension matters**, without claiming that the deployed cache design exhibited that failure.

### Verification Evidence

The development-agent report records **15/15 focused integration tests passing**, **32/32 passing** when combined with the existing cache suite, and **328/328 passing** in the reported full local run against PostgreSQL and Redis. Results are documented in [`docs/REDIS_FAULT_INJECTION_CASE_STUDY.md`](https://github.com/duyhunga1k45pbc-cloud/personal-finance-platform/blob/main/docs/REDIS_FAULT_INJECTION_CASE_STUDY.md); the implementation tests are in `backend/tests/test_redis_fault_injection_correctness.py`.

**Outcome:** No violation of these selected invariants was reproduced in the current implementation under the tested failure conditions. This is **failure-oriented verification, not a new root-cause analysis of a production defect**. The concurrent refund outcome is expected because Redis is outside the financial write path.

**Limitations:** The tests do not cover every possible scheduling interleaving, real infrastructure failures, or a compromised Redis instance serving a deliberately forged but well-formed payload. CRC32 framing detects accidental corruption, not malicious data substitution. The results are local and do not establish production readiness.

---

## Case Study: Connection Pool Starvation

### The Symptom and Root Cause

During Redis benchmarks, throughput degraded sharply under higher concurrency despite little active PostgreSQL query work. Instrumentation showed that a request could hold **two database connections simultaneously**: a FastAPI authentication dependency checked out one, then business logic attempted to check out a second before the first was returned.

At sufficiently high concurrency this produced **connection pool starvation**, not a slow SQL query or database row-lock bottleneck.

### Evaluated Approaches

| **Approach**                                         | **Observed result**                           | **Trade-off**                                                                                                    |
| ---------------------------------------------------- | --------------------------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| Overlapping authentication and business sessions     | Pool starvation under the tested load         | Separate transactions but unnecessary simultaneous connections                                                   |
| Single shared session                                | Removed observed pool exhaustion              | Authentication and business share transaction lifecycle; business rollback can expire ORM user state             |
| **Short-lived auth session + immutable `Principal`** | **Selected; no pool timeouts in tested runs** | Sequential connection checkouts with separate transaction boundaries; the pool may reuse one physical connection |

The selected design validates the JWT, loads the user in a short-lived authentication session, copies the needed attributes to an immutable `Principal`, **closes the authentication session**, and only then opens the business session. Authorization consumes verified identity values rather than a live ORM `User` object.

### Verification and Limitations

Lifecycle tests verify that authentication returns its connection before business checkout and that error and rollback paths preserve the intended boundary. Concurrent runs through **32 virtual users** reported no pool timeouts after the change. This establishes the tested lifecycle improvement, **not** unlimited throughput or production availability.

**Implementation references:** `backend/app/routers/auth.py`, `backend/tests/test_auth_session_lifecycle.py`.

---

## Case Study: Deterministic Pagination and PostgreSQL Indexing

An index audit of the transaction-listing endpoint uncovered **two separate issues**: an expensive ownership-scoped query for some users and an API response whose page ordering was not guaranteed.

### The Correctness Boundary

The original `GET /transactions` query applied `LIMIT` and `OFFSET` without an explicit `ORDER BY`. PostgreSQL does not guarantee row order in this case. On a seeded audit database, changing the join plan changed which transaction IDs appeared at the same offset, even though the underlying data was unchanged.

Phase A defines a complete ordering contract using **`transactions.id ASC`**, with the primary key serving as a unique tie-breaker. This improves deterministic paging **for an unchanged dataset**; it does not prevent offset drift when concurrent inserts occur between requests. Ascending ID also means the first 100 results are the **oldest by ID**, not the newest, which is relevant to Moneta's presentation.

### The Query and Index Decision

The application filters the listing by `user_id` and now sorts by `id`:

```
SELECT ...
FROM transactions
JOIN financial_events ON ...
WHERE transactions.user_id = :user_id
ORDER BY transactions.id ASC
LIMIT :limit OFFSET :skip;
```

This is a **schematic excerpt** of the application query, not an exact copy of SQLAlchemy's full generated SQL; the benchmark replay uses the generated SQL, including the real joins and filters.

The audit then evaluated a composite B-tree index on **`transactions(user_id, id)`**. The index was added in migration `l2c0d7e4a588` using `CREATE INDEX CONCURRENTLY` inside an Alembic autocommit block, with explicit handling of an interrupted invalid build. Upgrade, downgrade and repeat upgrade were tested on a disposable database.

### Measurement and Trade-offs

Selected `EXPLAIN (ANALYZE, BUFFERS)` measurements from a synthetic PostgreSQL audit database containing about **498,000 transactions** (median of nine warm-cache runs):

| **Request/query component**                     | **Before index** | **After index** |
| ----------------------------------------------- | ---------------- | --------------- |
| User with \~100 transactions, first page        | 21.93 ms         | 0.02 ms         |
| User with \~10,000 transactions, first page     | 27.24 ms         | 19.24 ms        |
| User with \~10,000 transactions, offset 5,000   | 36.77 ms         | 20.80 ms        |
| User with \~100,000 transactions, offset 50,000 | 17.66 ms         | 17.60 ms        |

These are **SQL execution measurements**, not HTTP end-to-end latency, and performance varied with user cardinality and plan choice. The index occupied approximately **11 MB** on the seeded database; one insert experiment showed approximately **10% additional write time**. Large offsets and the `COUNT` join for very large users remain expensive.

### Verification and Scope

Five new API pagination tests cover ordering, page boundaries, filters, authorization and ownership. They all passed, but **also passed when `ORDER BY` was temporarily removed** on the small test fixture; therefore, the separate large-dataset plan-change reproduction is the stronger evidence for the original non-determinism. The change was **merged into `main` via PR #6 on 2026-10-12**. That merge does not, by itself, verify the CI status of the latest Redis-fault-injection revision.

The same audit identified **four redundant non-unique indexes** whose key definitions overlap primary-key or unique-constraint indexes. They were **not removed in Phase A**. The associated database constraints remain in place; cleanup is deferred and would require its own reviewed migration.

**Implementation references:** `backend/app/routers/transactions.py`, `backend/alembic/versions/l2c0d7e4a588_transactions_user_id_index.py`, `backend/tests/test_transactions_pagination.py`, and `docs/transactions_index/README.md` (created during the Phase A audit).

---

## System Structure

The application is implemented as a **modular monolith**.

Its design keeps financial business rules and transaction boundaries explicit without introducing distributed infrastructure that is unnecessary for the project's current scope.

The main responsibilities are separated into:

| **Component**             | **Responsibility**                                                                  |
| ------------------------- | ----------------------------------------------------------------------------------- |
| FastAPI routers           | HTTP request handling, authentication dependencies, validation, and responses       |
| Business services         | Financial operations, domain invariants, causal rules, and transaction coordination |
| Canonical financial state | Authoritative financial events, entries, and relationships                          |
| Financial history         | Persistent historical evidence of state changes                                     |
| Provider integration      | External data ingestion, normalization, and reconciliation                          |
| Derived projections       | Rebuildable balances and financial summaries                                        |
| Operational mechanisms    | Idempotency, readiness, migrations, auditing, and recovery                          |

A simplified flow distinguishes external evidence from validated financial state:

```
External Provider Evidence                 Client Financial Commands (API)
             |                                         |
             v                                         |
   Identity / Deduplication                            |
             |                                         |
             v                                         |
        Normalization                                  |
             |                                         |
             v                                         |
  Canonical Interpretation                             |
             |                                         |
             +--------------------+--------------------+
                                  |
                                  v
                       Validated Transition
                                  |
                                  v
                    PostgreSQL Transaction / COMMIT
                                  |
                                  v
                       Canonical Financial State
                                  |
                            +-----+-----+
                            |           |
                            v           v
                     Append-only    Rebuildable
                        History      Projections

```

Normalization alone does **not** make provider observations authoritative financial events. Interpretation and validated transitions are distinct responsibilities; external evidence can also be reconciled against canonical records without being silently accepted. Client-initiated commands enter via FastAPI and must respect the same domain invariants and explicit transaction boundaries.

Cross-cutting concerns include authentication, authorization, idempotency, concurrency control, auditability, and operational recovery.

The project does not claim to implement a production distributed event-processing platform.

---

## Core Engineering Properties

### 1. Financial Domain Invariants

Financial operations are represented through explicit domain semantics instead of treating all transactions as interchangeable records.

Supported concepts include:

- Income and expenses
- Transfers between accounts
- Refunds and reversals
- Corrections and void operations
- Reconciliation adjustments
- Canonical events and signed financial entries

The implementation uses these distinctions to preserve financial meaning across state transitions.

Business invariants are enforced within the application and verified through database-backed tests.

### 2. Transactional Idempotency

Financial commands use database-backed idempotency records to prevent the same command from applying its effects repeatedly.

Verification includes:

- Repeated requests with the same command identity
- Conflicting reuse of an idempotency key
- Concurrent same-key commands
- Successful commits followed by simulated response loss
- Recovery of previously committed command responses

The intended guarantee is that retrying an already committed command does not duplicate its financial effect.

### 3. Concurrency and Version Control

The system uses PostgreSQL transaction semantics to protect financial state against selected concurrent modifications.

Implemented mechanisms include:

- Expected-version checks
- Row-level locking
- Shared original-event serialization for causal commands
- Stale-write rejection
- Database-backed concurrency regression tests

Tests exercise actual PostgreSQL lock contention rather than relying exclusively on mocked database behavior.

The verified guarantees depend on the transaction boundaries and isolation assumptions exercised by those tests.

### 4. Historical Integrity and Causal Relationships

Canonical financial events, signed entries, and append-only historical records provide evidence of financial state evolution.

Causal relationships connect operations such as refunds and reversals to their original financial events.

Historical evidence supports:

- State-transition verification
- Canonical audits
- Investigation of inconsistent financial relationships
- Recovery and reconciliation workflows

The application distinguishes recorded financial history from derived representations of that history.

### 5. Explicit Reconciliation

External financial evidence and accepted canonical state are treated as separate concerns.

External observations can be stale, incomplete, duplicated, or contradictory.

Reconciliation workflows preserve the distinction between:

- What an external provider reports
- What the application currently records
- What has been accepted as canonical financial state

Conflicting evidence is surfaced rather than silently converted into an authoritative financial update.

### 6. Rebuildable Projections

Balances and financial summaries are derived from canonical financial records.

Projection tests verify that derived data can be removed, reconstructed, and checked against its source.

This reduces dependence on projections as independent sources of financial truth.

### 7. Security Boundaries

The application implements JWT-based authentication and user-scoped authorization.

Security verification includes:

- Invalid and tampered tokens
- Invalid credentials
- Deleted-user identities
- Unauthorized cross-user reads
- Unauthorized cross-user modifications
- Ownership checks on financial resources

The application does not claim to provide a complete OAuth2/OIDC identity platform or a shared multi-service authorization framework.

#### Authorization architecture

Authentication and authorization are separate steps. JWT validation establishes **who is calling**; a valid token alone grants nothing beyond the caller's own data. Protected routes use an immutable, verified `Principal` obtained through a short-lived authentication session that closes before business logic checks out its own connection.

- **Policy model** (`backend/app/authorization.py`): a `Subject` (the authenticated user id) acts with an `Action` on a `Resource` (a `ResourceType` plus its owner id). The only rule is ownership; there are no roles, scopes or admin permissions.
- **Route policy table:** every HTTP operation has one `ROUTE_POLICIES` entry classified as `PUBLIC`, `OWNER` (resource + action) or `METRICS_TOKEN`.
- **Runtime enforcement:** an app-wide dependency returns 403 for any HTTP route without a declared policy. Owner-scoped routers carry a router-level guard that authenticates, then requires the route's own policy to be a fully specified `OWNER` policy. Object-level checks (`authorize_object`) are bound to that same route policy and cannot borrow a grant from another route.
- **SQL ownership scoping:** `owned(query, column, subject)` applies the ownership predicate in SQL, before joins, counts, pagination and aggregation. It is used by the account, provider-connection, active-transaction and projection queries. Existing row-locking queries are unchanged.
- **Contracts preserved:** 401 for missing or invalid credentials, 404 for another user's object, and 422 for an unowned refund or reversal original event, identical to a non-existent one.

Boundaries of this enforcement:

- It covers HTTP `APIRoute` handlers only. Mounted sub-apps, WebSocket routes and raw routes are prohibited by a startup inventory check (`assert_only_policed_routes`), not protected by it; adding one requires its own reviewed enforcement.
- Ownership checks remain decentralized. Many service-layer and locked queries still filter by `user_id` directly, and only the account, transaction and provider-connection single-object reads call `authorize_object`. The policy layer is an additional check, not a replacement for those queries.
- The policy table and handlers are kept consistent by tests, not by construction.

The public `GET /demo` page is also declared in `ROUTE_POLICIES`; the HTML shell is public, but every data API it calls remains independently authenticated and authorized.

See `backend/SECURITY_V1.md` for details. Verification is PostgreSQL-backed (`backend/tests/test_authorization_policy.py`): cross-user reads and writes with no database side effects, nested-resource checks, pagination and aggregation isolation, policy-change tests, and query-count and connection-checkout baselines.

### 8. Schema Evolution Safety

Database schema changes are managed with Alembic.

Verification includes:

- Transactional rollback after injected migration failures
- Preservation of historical financial meaning during canonical-state backfill
- Readiness rejection of incompatible schema revisions
- Migration tests using disposable PostgreSQL databases
- Concurrent index creation in a separate Alembic autocommit block (merged Phase A work), including downgrade and invalid-index recovery checks

The test infrastructure rejects the protected `finance_db` database as a pytest target.

Production-target migrations are blocked by default and require explicit per-invocation authorization.

These protections do not establish zero-downtime or rolling-upgrade compatibility.

---

## Test Results and Coverage

The repository includes unit, integration, concurrency, failure-injection, and end-to-end tests. Database-sensitive scenarios use PostgreSQL; optional cache tests include Redis integration. The reported outcomes apply only to the conditions exercised.

### Latest Reported Local Regression

| **Result**                             | **Value**                                                                                    |
| -------------------------------------- | -------------------------------------------------------------------------------------------- |
| Full local suite (reported)            | **328 passed, 0 failed, 0 skipped**                                                          |
| Redis fault-injection focus (reported) | **15 passed**                                                                                |
| Cache suites combined (reported)       | **32 passed**                                                                                |
| Database                               | Disposable PostgreSQL migrated to Alembic head                                               |
| Cache tests                            | Real Redis integration included                                                              |
| Source of results                      | Development-agent local execution report; not independently rerun for this README edit       |
| GitHub CI for the 328-test revision    | **Not verified here**; check the commit-specific GitHub Actions run before claiming green CI |

The reported results were obtained in a local verification environment. They do not imply successful production deployment or universal coverage of runtime failure modes.

### Verification Coverage

| **Area**                | **Selected scenarios**                                                                                          |
| ----------------------- | --------------------------------------------------------------------------------------------------------------- |
| Financial semantics     | Canonical events, entries, transfers, refunds, reversals, corrections                                           |
| Transaction correctness | Idempotent replay, competing commands, expected-version conflicts                                               |
| Concurrency             | Row locking, lock contention, stale reads, refund/reversal exclusion                                            |
| Redis fault injection   | Controlled stale-fill race, unversioned-key counterfactual, read/write failures, fallback, cross-user isolation |
| Provider integration    | Evidence handling, lifecycle state, checkpoints, reconciliation                                                 |
| Derived state           | Projection rebuilding and canonical consistency                                                                 |
| Security                | JWT validation, ownership, cross-user authorization                                                             |
| Runtime failures        | Response loss, worker termination, pool exhaustion, deadlocks                                                   |
| Recovery                | Backup/restore, projection reconstruction, canonical audits                                                     |
| Deployment              | Preflight checks, revision compatibility, readiness, graceful shutdown                                          |
| Schema evolution        | Rollback, historical backfill invariants, incompatible revisions                                                |
| Async execution         | AsyncSession behavior, cancellation, session ownership, lock contention                                         |
| Middleware              | Cancellation propagation and request-context isolation                                                          |

---

## Runtime Failure Verification

Fault-injection exercises include:

- Simulated HTTP response loss after successful database commit
- Idempotent retry following an ambiguous response outcome
- Connection-pool exhaustion and recovery
- Worker termination before and after transaction commit
- PostgreSQL deadlock recovery using SQLSTATE `40P01`
- Verification of canonical records, historical entries, receipts, and provider checkpoints after failure

Worker-process failure scenarios are exercised directly.

The reported tests do not establish equivalent behavior for actual PostgreSQL server crashes, infrastructure outages, or real network and reverse-proxy disconnects.

---

## Async Execution Verification

The application's primary financial services use synchronous SQLAlchemy.

Separate experiments investigate whether selected correctness contracts remain valid under different asynchronous execution models.

### SQLAlchemy AsyncSession Bridge

Five isolated PostgreSQL-backed tests evaluate execution through `AsyncSession.run_sync()`.

The scenarios cover:

- Cancellation before commit
- Simulated response loss after commit
- Conflicting guarded writes
- Connection-pool exhaustion and recovery
- Independent session ownership and transaction isolation

These tests verify selected async-driver and transaction-lifecycle behavior.

They do not constitute a native-async rewrite of the application.

### Native-Async Financial Operations

Five additional PostgreSQL-backed tests exercise a **test-only native-async financial correction implementation** using `async def`, `await`, and SQLAlchemy `AsyncSession`.

The tested scenarios include:

1. Correct financial state transitions and idempotent replay
2. Cancellation before commit and rollback verification
3. Simulated post-commit acknowledgment loss
4. Concurrent guarded writes with PostgreSQL lock contention
5. Connection-pool exhaustion and recovery

**Result:** 5/5 native-async experimental tests passed.

The native-async implementation is experimental and is not the application's production correction endpoint.

### ASGI Middleware Behavior

Additional tests exercise actual ASGI middleware execution with controlled asynchronous coroutines.

They verify:

- Cancellation propagation
- Request-context cleanup
- Request-ID `ContextVar` reset
- Concurrent request-context isolation
- Expected process-local request metrics

These tests do not establish behavior for every real client disconnect or cancellation of synchronous database operations.

---

## Operational Verification

Additional exercises cover:

- PostgreSQL backup and restore
- Projection deletion and rebuilding
- Canonical-state auditing after recovery
- Deployment preflight validation
- Uvicorn process smoke testing
- Liveness and readiness endpoints
- Graceful shutdown through SIGTERM
- Detection of incompatible database revisions

Operational verification focuses on making tested failure outcomes observable and recoverable.

The project has not undergone production traffic, availability, or incident-response validation.

---

## Moneta Demo UI

Moneta is a lightweight **HTML/CSS/JavaScript interface** served at `GET /demo`. It is a presentation layer over existing APIs, **not** the core engineering deliverable.

| **Screen**     | **Data source**                              | **V1 boundary**                                                                 |
| -------------- | -------------------------------------------- | ------------------------------------------------------------------------------- |
| Login          | `POST /auth/login`                           | Existing account required; no hard-coded demo identity                          |
| Dashboard      | `GET /summary`                               | Shows current API summary, not all canonical projections                        |
| Accounts       | `GET /accounts`, `GET /projections/accounts` | Missing projections are shown explicitly, not fabricated                        |
| Transactions   | `GET /transactions`                          | Read-only; first 100 **oldest-by-ID** results under the merged Phase A ordering |
| Reconciliation | `GET /reconciliations/{id}`                  | Lookup by ID; no listing endpoint                                               |
| Engineering    | Static explanation                           | Not a live operations console                                                   |

The browser keeps JWTs **only in memory**; logout clears displayed personal data. API errors yield visible error/empty states instead of a fake-data fallback. The UI intentionally does not initiate financial writes in v1.

**Implementation references:** `backend/static/moneta.html`, `backend/tests/test_moneta_demo.py`.

---

## Technology Stack

| **Layer**                      | **Technology**                                                 |
| ------------------------------ | -------------------------------------------------------------- |
| Language                       | Python                                                         |
| API framework                  | FastAPI                                                        |
| Validation                     | Pydantic                                                       |
| Database                       | PostgreSQL                                                     |
| ORM                            | SQLAlchemy                                                     |
| Schema migrations              | Alembic                                                        |
| Authentication / authorization | JWT, declarative route policy, short-lived immutable Principal |
| Testing                        | pytest, PostgreSQL and Redis integration tests                 |
| Async verification             | asyncio, AsyncSession, asyncpg                                 |
| Application server             | Uvicorn                                                        |
| CI configuration               | GitHub Actions                                                 |
| Optional response cache        | Redis                                                          |
| Demo                           | HTML, CSS, JavaScript (Moneta)                                 |

The application primarily uses synchronous financial services. Native-async financial behavior is evaluated separately through isolated experiments.

---

## Running Locally

### Prerequisites

- Python and pip
- PostgreSQL
- Redis (optional for runtime, needed for cache integration tests)
- A dedicated development database
- A separate disposable database for testing

Some tests require PostgreSQL database-creation privileges or additional privileges for fault-injection scenarios.

### Install Dependencies

From the `backend` directory:

```
pip install -r requirements.txt
pip install -r requirements-test.txt
```

### Configure PostgreSQL

Set `DATABASE_URL` to a PostgreSQL database intended for the current operation.

For example:

```
export DATABASE_URL="postgresql://USER:PASSWORD@localhost:5432/finance_dev_db"
```

Use a separate, disposable database for tests.

### Apply Migrations

```
alembic upgrade head
```

Migrations targeting the protected `finance_db` database require explicit authorization as documented in `backend/DEPLOYMENT_V1.md`.

Do not bypass database-safety checks when preparing a test environment.

### Run Tests

From the `backend` directory, use a **disposable** PostgreSQL test database:

```
export DATABASE_URL="postgresql://USER:PASSWORD@localhost:5432/finance_test_db"
python -m pytest -q
```

To reproduce the Redis-related suite, start a local Redis instance and use a **dedicated disposable Redis database** (database 15 is an example, not a requirement):

```
export REDIS_TEST_URL="redis://127.0.0.1:6379/15"
export REQUIRE_REDIS_TESTS=1

python -m pytest -q tests/test_redis_fault_injection_correctness.py
python -m pytest -q
```

The Redis integration fixtures may **flush the configured Redis test database**. Never use a shared or valuable Redis database. The reported 328-test result is local, environment-dependent evidence; the complete suite may require additional PostgreSQL privileges for some experiments.

**Warning:** Tests may create, modify, or destroy data. Never point the test runner at a database containing important records.

### Start the API

```
uvicorn app.main:app --reload --port 8765
```

Visit `http://127.0.0.1:8765/demo` for Moneta or `http://127.0.0.1:8765/docs` for Swagger. Register an account using `POST /auth/register` before logging in to a fresh environment. Moneta will show an empty financial history until actual transactions exist.

Redis is optional for the running application: set `REDIS_URL` only when provider listing caching is needed. Integration tests use the separate `REDIS_TEST_URL` and `REQUIRE_REDIS_TESTS=1` settings shown above.

---

## Scope and Limitations

Personal Finance Platform is an engineering case study, not a production-deployed financial product.

The current work does not establish:

- Exhaustive coverage of concurrent transaction interleavings
- Correctness under every PostgreSQL isolation level
- Complete resilience against infrastructure or network outages
- Security against deliberate cache poisoning or a compromised Redis server; the payload checksum is not an authenticity guarantee
- Zero-downtime schema migration compatibility
- A fully native-async production application
- A distributed financial event-processing platform
- Complete OAuth2/OIDC or organization-wide authorization infrastructure
- Production-grade performance, availability, or operational readiness
- Reliable cache hit rates under unknown real-world workloads
- Pagination for extremely large provider listing histories
- Stable offset pagination under concurrent writes; `GET /transactions` uses ID-ascending order and may require a separate newest-first UI query contract
- Proven improvement for every database workload: the new composite index adds write/storage cost and offers limited gains for some high-cardinality users
- Full financial write/reconciliation workflows from the Moneta UI

Some guarantees also depend on application write paths following shared locking and transaction conventions. Direct database writes or future code paths that bypass those conventions require separate verification.

The public repository emphasizes implemented behavior, reproducible tests, documented engineering decisions, and explicit limitations. In particular, the Redis fault-injection results are evidence for **selected failures survived**, not a proof that Redis cannot contribute to future correctness defects.

**Development disclosure:** GitHub Copilot assisted with portions of implementation, test creation, and documentation.
