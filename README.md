# Personal Finance Platform

**A correctness-focused financial backend engineering case study built with FastAPI, PostgreSQL, and SQLAlchemy.**

This project explores how financial state can remain consistent and recoverable when requests retry, concurrent operations conflict, transactions fail, external evidence disagrees, or execution conditions change.

It started as a conventional income/expense CRUD application and evolved into a backend engineering project focused on **business invariants, transaction boundaries, concurrency control, idempotency, recovery, and evidence-based verification**. Subsequent work investigated **cache consistency, authorization boundaries, database connection ownership, and performance measurement**.

Rather than optimizing for feature count, the project emphasizes understanding failure modes, implementing explicit correctness guarantees, and testing those guarantees against real PostgreSQL behavior.

**Project status:** Engineering portfolio and experimental application. Not production-deployed.

**Quick navigation:** [Over-refund case study](#case-study-preventing-concurrent-over-refunds) · [Redis revision cache](#case-study-transaction-consistent-redis-caching) · [Short-lived auth sessions](#case-study-connection-pool-starvation) · [Index & pagination](#case-study-deterministic-pagination-and-postgresql-indexing) · [Moneta demo](#moneta-demo-ui) · [Verification](#verification-strategy)

## Engineering Highlights

| Area | Implemented and verified behavior |
|---|---|
| Financial domain modeling | Canonical financial events, signed entries, transfers, refunds, reversals, and corrections |
| Concurrency control | PostgreSQL row locking, expected-version guards, deterministic conflicting transactions |
| Transactional idempotency | Database-backed command receipts, retry recovery, concurrent same-key replay |
| Historical integrity | Append-only financial history and causal event relationships |
| Reconciliation | Explicit handling of conflicting and incomplete external evidence |
| Recovery | Rebuildable projections, canonical-state audits, backup/restore exercises |
| Failure injection | Commit acknowledgment loss, worker termination, deadlocks, connection-pool exhaustion |
| Security | JWT authentication, declared route policies, SQL ownership scoping, short-lived auth sessions |
| Schema evolution | Alembic migrations, rollback verification, historical backfill, revision compatibility checks |
| Async correctness | SQLAlchemy AsyncSession, cancellation, connection lifecycle, native-async experiments |
| Read performance | N+1 removal, PostgreSQL-revisioned Redis cache, query-plan analysis and controlled benchmarks |
| Query correctness | Deterministic transaction pagination and a measured composite-index optimization (Phase A branch; merge pending) |
| Demo | Moneta read-only web interface over existing authenticated FastAPI APIs |

**Latest reported local regression:** **294 passed, 0 failed, 0 skipped** on isolated PostgreSQL with real Redis tests enabled (**Phase A worktree**, commit `37d3f8b`; not yet confirmed merged into `main`). The earlier Moneta worktree reported **289 passed**, and its merged PR passed GitHub Actions. These results belong to different code revisions and test collections; check CI for the Phase A PR separately. Older **208-test** reports covered an earlier, smaller production-suite scope.

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

At that stage, the selected production test suite passed **208 tests** on an isolated PostgreSQL database. Later regressions reported **289 passed** on the Moneta worktree and **294 passed** on the Phase A worktree under different revision/test scopes (see [Verification Strategy](#verification-strategy)).

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

```text
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

**Implementation references:** `backend/app/listing_cache.py`, migration `k1b9c6d3f477`, `backend/tests/test_provider_listing_cache.py`.

---

## Case Study: Connection Pool Starvation

### The Symptom and Root Cause

During Redis benchmarks, throughput degraded sharply under higher concurrency despite little active PostgreSQL query work. Instrumentation showed that a request could hold **two database connections simultaneously**: a FastAPI authentication dependency checked out one, then business logic attempted to check out a second before the first was returned.

At sufficiently high concurrency this produced **connection pool starvation**, not a slow SQL query or database row-lock bottleneck.

### Evaluated Approaches

| Approach | Observed result | Trade-off |
|---|---|---|
| Overlapping authentication and business sessions | Pool starvation under the tested load | Separate transactions but unnecessary simultaneous connections |
| Single shared session | Removed observed pool exhaustion | Authentication and business share transaction lifecycle; business rollback can expire ORM user state |
| **Short-lived auth session + immutable `Principal`** | **Selected; no pool timeouts in tested runs** | Two *sequential* connections, while preserving separate transaction boundaries |

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

```sql
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

| Request/query component | Before index | After index |
|---|---:|---:|
| User with ~100 transactions, first page | 21.93 ms | 0.02 ms |
| User with ~10,000 transactions, first page | 27.24 ms | 19.24 ms |
| User with ~10,000 transactions, offset 5,000 | 36.77 ms | 20.80 ms |
| User with ~100,000 transactions, offset 50,000 | 17.66 ms | 17.60 ms |

These are **SQL execution measurements**, not HTTP end-to-end latency, and performance varied with user cardinality and plan choice. The index occupied approximately **11 MB** on the seeded database; one insert experiment showed approximately **10% additional write time**. Large offsets and the `COUNT` join for very large users remain expensive.

### Verification and Scope

Five new API pagination tests cover ordering, page boundaries, filters, authorization and ownership. They all passed, but **also passed when `ORDER BY` was temporarily removed** on the small test fixture; therefore, the separate large-dataset plan-change reproduction is the stronger evidence for the original non-determinism. Phase A's full local regression reported **294 passed, 0 failed, 0 skipped** with PostgreSQL and Redis. These results apply to commit `37d3f8b` on `feat/transactions-index-pagination`; **merge and GitHub CI status must be checked separately**.

The same audit identified **four redundant non-unique indexes** whose key definitions overlap primary-key or unique-constraint indexes. They were **not removed in Phase A**. The associated database constraints remain in place; cleanup is deferred and would require its own reviewed migration.

**Implementation references:** `backend/app/routers/transactions.py`, `backend/alembic/versions/l2c0d7e4a588_transactions_user_id_index.py`, `backend/tests/test_transactions_pagination.py`, `docs/transactions_index/README.md` (Phase A branch).

---

## System Structure

The application is implemented as a **modular monolith**.

Its design keeps financial business rules and transaction boundaries explicit without introducing distributed infrastructure that is unnecessary for the project's current scope.

The main responsibilities are separated into:

| Component | Responsibility |
|---|---|
| FastAPI routers | HTTP request handling, authentication dependencies, validation, and responses |
| Business services | Financial operations, domain invariants, causal rules, and transaction coordination |
| Canonical financial state | Authoritative financial events, entries, and relationships |
| Financial history | Persistent historical evidence of state changes |
| Provider integration | External data ingestion, normalization, and reconciliation |
| Derived projections | Rebuildable balances and financial summaries |
| Operational mechanisms | Idempotency, readiness, migrations, auditing, and recovery |

A simplified financial data flow is:

```text
External Financial Providers
             |
             v
       Data Ingestion
             |
             v
        Normalization
             |
             v
   Canonical Financial State
             |
       +-----+------+
       |            |
       v            v
  Event History   Derived
                  Projections
       |            |
       +-----+------+
             |
             v
         FastAPI API
```

Client-initiated financial commands enter through the API and are processed by business services within explicit database transaction boundaries.

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
- Concurrent index creation in a separate Alembic autocommit block (Phase A branch), including downgrade and invalid-index recovery checks

The test infrastructure rejects the protected `finance_db` database as a pytest target.

Production-target migrations are blocked by default and require explicit per-invocation authorization.

These protections do not establish zero-downtime or rolling-upgrade compatibility.

---

## Verification Strategy

The project follows a failure-oriented verification approach. The **208-test** earlier baseline, **289-test** Moneta worktree run and **294-test** Phase A worktree run span different code revisions and collection scopes; their totals must not be treated as a direct quality metric.

1. Identify the business behavior and its correctness invariant.
2. Determine which transaction, concurrency, or execution conditions could violate it.
3. Construct a reproducible scenario.
4. Verify actual behavior against PostgreSQL where database semantics matter.
5. Introduce or correct the protection mechanism.
6. Rerun the scenario and relevant regression tests.
7. Record the verified guarantee and its limitations.

Tests are treated as evidence for defined claims, not proof that every possible system state has been explored.

### Latest Reported Local Regression

| Result | Value |
|---|---|
| Passed | **294** |
| Failed / skipped | **0 / 0** |
| Database | Disposable PostgreSQL migrated to Alembic head |
| Cache tests | Real Redis integration included |
| Scope | Phase A worktree (`feat/transactions-index-pagination`, commit `37d3f8b`) |
| GitHub CI | Moneta PR checks passed previously; Phase A PR CI/merge **not yet verified** |

Earlier experiments and local-only research artifacts have separate test scopes. The latest count above must not be interpreted as including every private experimental test.

The reported results were obtained in a local verification environment. They do not imply successful production deployment or universal coverage of runtime failure modes.

### Verification Coverage

| Area | Selected scenarios |
|---|---|
| Financial semantics | Canonical events, entries, transfers, refunds, reversals, corrections |
| Transaction correctness | Idempotent replay, competing commands, expected-version conflicts |
| Concurrency | Row locking, lock contention, stale reads, refund/reversal exclusion |
| Provider integration | Evidence handling, lifecycle state, checkpoints, reconciliation |
| Derived state | Projection rebuilding and canonical consistency |
| Security | JWT validation, ownership, cross-user authorization |
| Runtime failures | Response loss, worker termination, pool exhaustion, deadlocks |
| Recovery | Backup/restore, projection reconstruction, canonical audits |
| Deployment | Preflight checks, revision compatibility, readiness, graceful shutdown |
| Schema evolution | Rollback, historical backfill invariants, incompatible revisions |
| Async execution | AsyncSession behavior, cancellation, session ownership, lock contention |
| Middleware | Cancellation propagation and request-context isolation |

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

These experiments are distinct from the earlier 208-test baseline and from the later 289-test Moneta worktree regression.

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

| Screen | Data source | V1 boundary |
|---|---|---|
| Login | `POST /auth/login` | Existing account required; no hard-coded demo identity |
| Dashboard | `GET /summary` | Shows current API summary, not all canonical projections |
| Accounts | `GET /accounts`, `GET /projections/accounts` | Missing projections are shown explicitly, not fabricated |
| Transactions | `GET /transactions` | Read-only; first 100 **oldest-by-ID** results under Phase A ordering (after merge) |
| Reconciliation | `GET /reconciliations/{id}` | Lookup by ID; no listing endpoint |
| Engineering | Static explanation | Not a live operations console |

The browser keeps JWTs **only in memory**; logout clears displayed personal data. API errors yield visible error/empty states instead of a fake-data fallback. The UI intentionally does not initiate financial writes in v1.

**Implementation references:** `backend/static/moneta.html`, `backend/tests/test_moneta_demo.py`.

---

## Technology Stack

| Layer | Technology |
|---|---|
| Language | Python |
| API framework | FastAPI |
| Validation | Pydantic |
| Database | PostgreSQL |
| ORM | SQLAlchemy |
| Schema migrations | Alembic |
| Authentication / authorization | JWT, declarative route policy, short-lived immutable Principal |
| Testing | pytest, PostgreSQL and Redis integration tests |
| Async verification | asyncio, AsyncSession, asyncpg |
| Application server | Uvicorn |
| CI configuration | GitHub Actions |
| Optional response cache | Redis |
| Demo | HTML, CSS, JavaScript (Moneta) |

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

```bash
pip install -r requirements.txt
pip install -r requirements-test.txt
```

### Configure PostgreSQL

Set `DATABASE_URL` to a PostgreSQL database intended for the current operation.

For example:

```bash
export DATABASE_URL="postgresql://USER:PASSWORD@localhost:5432/finance_dev_db"
```

Use a separate, disposable database for tests.

### Apply Migrations

```bash
alembic upgrade head
```

Migrations targeting the protected `finance_db` database require explicit authorization as documented in `backend/DEPLOYMENT_V1.md`.

Do not bypass database-safety checks when preparing a test environment.

### Run Tests

Use an isolated PostgreSQL test database:

```bash
export DATABASE_URL="postgresql://USER:PASSWORD@localhost:5432/finance_test_db"

pytest -q
```

The default test command may include experimental suites with additional environmental requirements. The reported 294-test Phase A worktree run has a different code revision/collection scope from the earlier 289-test Moneta run and private experimental suites.

**Warning:** Tests may create, modify, or destroy data. Never point the test runner at a database containing important records.

### Start the API

```bash
uvicorn app.main:app --reload --port 8765
```

Visit `http://127.0.0.1:8765/demo` for Moneta or `http://127.0.0.1:8765/docs` for Swagger. Register an account using `POST /auth/register` before logging in to a fresh environment. Moneta will show an empty financial history until actual transactions exist.

Redis is optional for the application: set `REDIS_URL` only when provider listing caching is needed. Cache integration tests require a **dedicated disposable Redis database** through `REDIS_TEST_URL` (tests may clear its contents); use `REQUIRE_REDIS_TESTS=1` to fail when those tests are skipped.

---

## Engineering Principles

This project is guided by the following principles:

**Preserve business meaning.** Financial state transitions must respect explicit domain relationships and invariants.

**Define transaction boundaries.** Atomic operations do not automatically guarantee correctness across concurrent transactions.

**Treat retries as normal execution.** A committed financial command must not be applied again because its original response was lost.

**Verify database behavior on a real database.** Mocked dependencies cannot reproduce every locking, isolation, commit, or rollback behavior.

**Separate observations from authority.** External evidence, canonical records, historical evidence, and derived projections have different responsibilities.

**Prefer prevention over retrospective detection.** Known invalid financial states should be rejected before commit whenever the system can enforce the required invariant.

**Make recovery testable.** Critical financial state should be auditable, and derived views should be reconstructable.

**State the limits of evidence.** A passing test establishes behavior under tested conditions, not correctness across every possible execution path.

**Separate identity from session lifetime.** Authentication connections should not stay checked out while the business handler acquires its own transaction.

**Do not treat cache TTL as a consistency mechanism.** PostgreSQL owns committed revisions; Redis stores derived responses only.

**Make query ordering explicit.** Database execution plans must not determine an API pagination contract.

**Control the cost of complexity.** Introduce mechanisms in response to explicit correctness requirements and demonstrated failure boundaries.

---

## Scope and Limitations

Personal Finance Platform is an engineering case study, not a production-deployed financial product.

The current work does not establish:

- Exhaustive coverage of concurrent transaction interleavings
- Correctness under every PostgreSQL isolation level
- Complete resilience against infrastructure or network outages
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

The public repository emphasizes implemented behavior, reproducible tests, documented engineering decisions, and explicit limitations.

## Portfolio Note

This repository is intended to demonstrate backend engineering through **concrete failure scenarios and verifiable correctness mechanisms**, rather than claims of production scale.

The primary emphasis is the relationship between:

**Business Requirements → Invariants → Failure Boundaries → Implementation → Verification Evidence**

The central engineering objective is not to eliminate all uncertainty, but to establish which correctness properties have been tested, under which conditions, and where additional evidence is still required.
