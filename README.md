# Personal Finance Platform

**A correctness-focused financial backend engineering case study built with FastAPI, PostgreSQL, and SQLAlchemy.**

This project explores how financial state can remain consistent and recoverable when requests retry, concurrent operations conflict, transactions fail, external evidence disagrees, or execution conditions change.

The backend implements financial event modeling, concurrency control, transactional idempotency, reconciliation, recovery tooling, authorization, and optional Redis caching. The repository includes PostgreSQL-backed integration tests, deterministic failure-injection scenarios, and performance measurements for selected operations.

**Project status:** Engineering portfolio and experimental application. Not production-deployed.

**Quick navigation:** [Over-refund](#case-study-preventing-concurrent-over-refunds) · [Redis revision cache](#case-study-transaction-consistent-redis-caching) · [Connection pool RCA](#case-study-connection-pool-starvation) · [Index & pagination](#case-study-deterministic-pagination-and-postgresql-indexing) · [Architecture](#system-structure) · [Security](#security-boundaries) · [Verification](#test-results-and-coverage)

---

## Engineering Highlights

| Area | Engineering evidence |
| --- | --- |
| **Financial correctness** | PostgreSQL locking, idempotent commands, append-only history, reconciliation |
| **Cache consistency** | PostgreSQL-revisioned Redis keys, ownership-before-cache checks, fault injection |
| **Query behavior** | Deterministic pagination and measured composite-index optimization |
| **Reliability** | Connection-pool lifecycle, failure injection, recovery and migration checks |
| **Security** | FastAPI policy and resource ownership enforcement; optional Kong/Keycloak integration labs |
| **Verification** | PostgreSQL and Redis integration tests, async experiments, gateway full-stack E2E |
| **Demo** | Moneta read-only interface backed by real financial APIs |

**Latest reported local backend suite:** **347 passed, 0 failed, 0 skipped in 136.83 seconds**, using PostgreSQL-backed tests and real Redis integration.

These local results are not tied to a verified commit-specific CI run. CI evidence is documented separately below.

The reported results apply to tested scenarios; they do not establish exhaustive correctness or production readiness.

---

## Case Study: Preventing Concurrent Over-Refunds

One of the project's most important findings was a reproducible concurrency defect in the refund workflow.

### The Business Invariant

For an original expense, the total amount of active refunds must not exceed the original refundable amount.

For example:

- Original expense: 100
- Refund request A: 60
- Refund request B: 60

**Both refunds must not succeed.**

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

The business invariant was already implemented for sequential execution. The missing guarantee was concurrency protection around the eligibility check and subsequent write.

### The Fix

The refund and reversal paths acquire a PostgreSQL `SELECT FOR UPDATE` row lock on the original financial event before evaluating eligibility.

The lock remains held until the transaction commits or rolls back.

Related correction and void operations follow the same original-event locking convention before evaluating causal dependencies.

Under the verified PostgreSQL `READ COMMITTED` isolation level, a competing transaction waits for the lock and then reevaluates eligibility against the committed state.

### Preserving Idempotency

The serialization change exposed a second interaction: concurrent retries using the same command identity may compete for the original-event lock.

After the winning request commits, the waiting request must recover the already committed result rather than fail merely because the refundable balance has changed.

The implementation includes receipt rechecking after causal rejection and rollback to preserve the same-key replay contract.

### Verification Evidence

A deterministic PostgreSQL regression test reproduces the original failure and verifies the corrected behavior.

The tests confirm:

- The vulnerable implementation permitted two successful 60-unit refunds against an expense of 100.
- The corrected implementation prevents both conflicting refunds from committing.
- PostgreSQL lock contention is observed.
- The persisted refund aggregate remains within the original amount.
- Concurrent same-key retries recover the winning response without duplicating financial effects.
- Competing refund and reversal commands respect the shared serialization boundary.
- Canonical records, causal links, historical records, and audit state remain consistent in the tested scenarios.

**Implementation references:**

- `backend/app/causal_service.py`
- `backend/app/canonical_service.py`
- `backend/app/routers/causal_events.py`
- `backend/tests/test_refund_concurrency.py`
- `backend/tests/test_async_sqlalchemy_correctness.py`

This case study documents a verified failure, root cause, implemented correction, and regression evidence. It does not claim that every possible concurrency defect has been eliminated.

---

## Case Study: Transaction-Consistent Redis Caching

A provider external-transaction listing had measurable read-side costs.

Removing an N+1 query pattern reduced database work, and a Redis prototype accelerated high-hit workloads.

However, a financial-data cache is useful only if it does not silently serve an outdated version after a committed write.

### The Failure Boundary

An invalidation sequence such as `COMMIT` followed by `Redis DEL` contains a crash window.

The database commit can succeed while cache invalidation never happens.

A TTL bounds how long the stale result persists; it does **not** establish read consistency.

### The Design

Each provider connection has a **listing revision stored in PostgreSQL**.

Database triggers advance the revision in the same transaction as inserts into provider transactions or evidence.

A rollback rolls back the revision as well.

```text
Provider writer transaction
  |
  +-- Insert transaction / evidence
  +-- Advance PostgreSQL listing revision
  |
  +-- COMMIT (or roll back both)
             |
             v
Authorized listing GET
  |
  +-- Verify user and connection ownership
  +-- Read committed listing revision
  +-- Build Redis key:
      user + connection + query + revision
                  |
           +------+------+
           |             |
         HIT            MISS
           |             |
           v             v
      Return cached   Query PostgreSQL
      response        and populate Redis
```

An in-flight reader can repopulate an older revision key, but subsequent requests look up the newer revision.

Redis outages fall back to PostgreSQL. **Ownership verification occurs before Redis access.**

The cache is optional and disabled without `REDIS_URL`.

### Verification and Trade-offs

PostgreSQL and Redis integration tests cover:

- Transaction rollback and savepoints
- Concurrent writers
- Cross-user isolation
- Stale in-flight cache fills
- Malformed cache data
- Redis failures

A controlled local experiment using 20 users, 300 transactions per connection, and 6 concurrent clients produced:

| Workload | Before Redis cache | After Redis cache |
| --- | ---: | ---: |
| Read-only traffic | 144 RPS | 271 RPS |
| Mixed read/write traffic | 126 RPS | 198 RPS |

These synthetic results depend on cache hit rate and are not production SLOs.

The design still makes PostgreSQL authorization and revision queries on cache hits, serializes revision updates for the same connection, and retains old Redis keys until TTL expiry.

**Implementation references:**

- `backend/app/listing_cache.py`
- Alembic migration `k1b9c6d3f477`
- `backend/tests/test_provider_listing_cache.py`
- `backend/tests/test_provider_listing_revision_concurrency.py`
- `backend/tests/test_redis_fault_injection_correctness.py`

---

## Case Study: Connection Pool Starvation

### The Symptom and Root Cause

During Redis benchmarks, throughput degraded sharply under higher concurrency despite little active PostgreSQL query work.

Instrumentation showed that a request could hold **two database connections simultaneously**:

1. A FastAPI authentication dependency checked out one connection.
2. Business logic attempted to check out another before the authentication connection was returned.

At sufficiently high concurrency, this produced **connection pool starvation**, not a slow SQL query or row-lock bottleneck.

### Evaluated Approaches

| Approach | Observed result | Trade-off |
| --- | --- | --- |
| Overlapping authentication and business sessions | Pool starvation under tested load | Unnecessary simultaneous connections |
| Single shared session | Removed observed pool exhaustion | Authentication and business share transaction lifecycle |
| **Short-lived auth session + immutable `Principal`** | **Selected; no pool timeouts in tested runs** | Sequential checkout with separate transaction boundaries |

### The Selected Design

The authentication path:

1. Validates the JWT.
2. Loads the user using a short-lived authentication session.
3. Copies required identity attributes into an immutable `Principal`.
4. Closes the authentication session.
5. Opens the business session afterward.

Authorization consumes verified identity values instead of a live ORM `User` object.

### Verification and Limitations

Lifecycle tests verify that authentication returns its connection before business checkout and that error and rollback paths preserve the intended boundary.

Concurrent runs through **32 virtual users** reported no pool timeouts after the change.

This establishes the tested lifecycle improvement, not unlimited throughput or production availability.

**Implementation references:**

- `backend/app/routers/auth.py`
- `backend/tests/test_auth_session_lifecycle.py`

---

## Case Study: Deterministic Pagination and PostgreSQL Indexing

An index audit of the transaction-listing endpoint uncovered two separate issues:

1. An expensive ownership-scoped query for some users.
2. An API response whose page ordering was not guaranteed.

### The Correctness Boundary

The original `GET /transactions` query applied `LIMIT` and `OFFSET` without an explicit `ORDER BY`.

PostgreSQL does not guarantee row order in this case.

On a seeded audit database, changing the join plan changed which transaction IDs appeared at the same offset, even though the underlying dataset was unchanged.

Phase A introduced a complete ordering contract using **`transactions.id ASC`**, with the primary key providing a unique ordering.

This improves deterministic pagination for an unchanged dataset. It does not prevent offset drift during concurrent inserts.

Ascending ID also means the first 100 results are the oldest by ID, not the newest.

### Query and Index Decision

The application filters by `user_id` and sorts by transaction ID.

Schematic query:

```sql
SELECT ...
FROM transactions
JOIN financial_events ON ...
WHERE transactions.user_id = :user_id
ORDER BY transactions.id ASC
LIMIT :limit OFFSET :skip;
```

The audit evaluated a composite B-tree index:

```sql
CREATE INDEX CONCURRENTLY
ON transactions (user_id, id);
```

The index was introduced through Alembic migration `l2c0d7e4a588`.

The migration handles an interrupted invalid index build and was tested through upgrade, downgrade, and repeat upgrade on a disposable database.

### Measurement and Trade-offs

Selected `EXPLAIN (ANALYZE, BUFFERS)` results from a synthetic PostgreSQL audit database containing approximately **498,000 transactions**:

Median of nine warm-cache runs.

| Request/query component | Before index | After index |
| --- | ---: | ---: |
| User with ~100 transactions, first page | 21.93 ms | 0.02 ms |
| User with ~10,000 transactions, first page | 27.24 ms | 19.24 ms |
| User with ~10,000 transactions, offset 5,000 | 36.77 ms | 20.80 ms |
| User with ~100,000 transactions, offset 50,000 | 17.66 ms | 17.60 ms |

These are **SQL execution measurements**, not end-to-end HTTP latency.

The index occupied approximately 11 MB on the seeded database, and one insert experiment showed approximately 10% additional write time.

Large offsets and expensive count queries remain unresolved for high-cardinality users.

### Verification and Scope

Five API pagination tests cover ordering, page boundaries, filters, authorization, and ownership.

However, the same five tests also passed when `ORDER BY` was temporarily removed on a small fixture. Therefore, the large-dataset join-plan reproduction provides stronger evidence for the original non-determinism.

The change was merged through [PR #6](https://github.com/duyhunga1k45pbc-cloud/personal-finance-platform/pull/6) on 2026-10-12.

The audit also identified four redundant non-unique indexes. They were not removed in Phase A.

**Implementation references:**

- `backend/app/routers/transactions.py`
- `backend/alembic/versions/l2c0d7e4a588_transactions_user_id_index.py`
- `backend/tests/test_transactions_pagination.py`
- `docs/transactions_index/README.md`

---

## System Structure

Personal Finance Platform is implemented as a **modular monolith**.

The architecture keeps financial business rules, transaction boundaries, and authoritative state explicit without introducing distributed infrastructure unnecessary for the project's current scope.

Kong Gateway and Keycloak are optional external integration components, not independently deployed financial business services.

### Component Responsibilities

| Component | Responsibility |
| --- | --- |
| FastAPI routers | HTTP handling, authentication dependencies, validation, responses |
| Business services | Financial operations, invariants, causal rules, transaction coordination |
| Canonical financial state | Authoritative events, entries, and relationships |
| Financial history | Persistent historical evidence of state changes |
| Provider integration | Ingestion, normalization, reconciliation |
| Derived projections | Rebuildable balances and summaries |
| Operational mechanisms | Idempotency, readiness, migrations, auditing, recovery |

### Financial State Flow

```text
External Provider Evidence      Client Financial Commands
          |                                |
          v                                |
   Identity / Deduplication                |
          |                                |
          v                                |
     Normalization                         |
          |                                |
          v                                |
 Canonical Interpretation                  |
          |                                |
          +---------------+----------------+
                          |
                          v
                 Validated Transition
                          |
                          v
              PostgreSQL Transaction
                          |
                        COMMIT
                          |
                          v
                Canonical Financial State
                          |
                   +------+------+
                   |             |
                   v             v
              Append-only    Rebuildable
                History      Projections
```

Normalization does **not** make provider observations authoritative financial events.

Interpretation and validated transitions are separate responsibilities. External evidence can also be reconciled against canonical records without silently being accepted as financial truth.

Client commands must respect the same financial domain invariants and explicit transaction boundaries.

The project does not claim to implement a production distributed event-processing platform.

---

## Core Engineering Properties

Financial events and signed entries are authoritative inputs for balances and summaries.

Derived projections are rebuildable from canonical records.

Financial commands use database-backed idempotency, expected-version guards, and PostgreSQL locking.

Reconciliation keeps external evidence distinct from accepted financial state, surfacing incomplete, stale, or conflicting observations rather than silently treating them as authoritative.

The refund case study illustrates how these mechanisms interact under concurrency.

### Security Boundaries

Authentication identifies the caller; authorization limits operations to owned resources.

The backend implements:

- Declared route authorization policies.
- Immutable authenticated `Principal` objects.
- SQL-level ownership filtering.
- Ownership checks before joins, counts, pagination, and aggregation.
- PostgreSQL-backed authorization and cross-user tests.

**Implementation references:**

- `backend/app/authorization.py`
- `backend/app/routers/auth.py`
- [`backend/SECURITY_V1.md`](backend/SECURITY_V1.md)

### Optional Gateway and Identity Labs

Kong and Keycloak are **optional integration experiments**, not prerequisites for Financial Core correctness.

The Gateway Lab has two explicit modes.

| Capability | HTTP Infrastructure Lab | External Authorization Lab |
| --- | --- | --- |
| Configuration | `gateway/compose.http.yml` | `gateway/compose.internal.yml` |
| API routing | Kong | Kong |
| JWT verification | Kong | Kong |
| Request correlation / infrastructure behavior | Enabled in HTTP Lab | Separate pilot configuration |
| External authorization | **Not used** | Kong external-authorization integration |
| Financial resource ownership | FastAPI | FastAPI |
| Financial domain invariants | Financial Core | Financial Core |

**HTTP Infrastructure Lab**

Kong handles API routing, request correlation IDs, IP-based rate limiting, and JWT verification.

It does not call the external authorization endpoint. FastAPI retains route authorization and SQL resource ownership enforcement.

**External Authorization Lab**

Preserves the earlier Kong authorization pilot, including fail-closed behavior under selected failures and route-specific policy enforcement.

This is not an organization-wide authorization platform or reusable cross-service authorization framework.

Both modes proxy to the same FastAPI backend. Neither changes financial transactions or canonical domain invariants.

See [`gateway/README.md`](gateway/README.md) for setup, login-route scope, limitations, and signing-key rotation.

### Schema and Migration Safety

Alembic verification covers transactional rollback, historical backfill, incompatible-revision readiness checks, concurrent index creation, downgrade, and interrupted invalid-index recovery.

Test database safeguards reject the protected `finance_db` target. Production migrations require explicit authorization.

These mechanisms do not establish zero-downtime schema evolution.

---

## Test Results and Coverage

The repository includes unit, integration, concurrency, failure-injection, and end-to-end tests.

Database-sensitive scenarios use PostgreSQL. Optional cache tests include real Redis integration.

### Local Test Results

| Verification | Reported result |
| --- | --- |
| Backend test suite | **347 passed, 0 failed, 0 skipped** |
| Kong authorization integration suite | **8 passed** |
| Gateway full-stack E2E | **2 passed** |
| Gateway Lab static configuration tests | **2 passed** |
| Targeted backend authorization/security/OIDC tests | **71 passed** |
| Native-async correction experiment | **5 passed** |

These are **separate test runs**. Their counts must not be summed into a single suite result.

The full backend result was obtained locally in 136.83 seconds using PostgreSQL-backed tests and real Redis.

### Gateway Verification

The Gateway Lab separation was merged into `main` through [PR #8](https://github.com/duyhunga1k45pbc-cloud/personal-finance-platform/pull/8), merge commit `e39eb79`.

The existing Gateway Full-Stack E2E and Kong Authorization Integration workflows passed on the feature branch.

A separate manual Docker exercise verified the **HTTP Infrastructure Lab**:

- Keycloak OIDC authentication through Moneta.
- Authenticated API reads through Kong.
- Protected `/auth/me`, `/accounts`, `/transactions`, and `/summary` requests returning successful responses.
- A request without a JWT rejected with HTTP `401`.
- No External AuthZ plugin required for the HTTP Infrastructure Lab.

This manual smoke test does not prove dedicated cross-user E2E coverage for the new HTTP mode.

### GitHub Actions Evidence

The following commit-specific CI results are separate from local test counts.

| Commit | Workflow | Result |
| --- | --- | --- |
| `4292cb2` | Kong Authorization Integration Tests | Passed |
| `4292cb2` | Gateway Full-Stack E2E | Passed |
| `4292cb2` | Backend CI | Passed |
| `27bb173` | Gateway Full-Stack E2E | Passed |
| `27bb173` | Backend CI | Passed |

The dedicated Kong integration workflow did not run for commit `27bb173`.

The authorization integration tests cover timeout, connection failure, malformed response, and HTTP 500 behavior under the External Authorization Lab.

The E2E suite verifies authenticated reads, a denied write without database mutation, and parity between deployment and E2E Kong configurations under the tested conditions.

These results are not evidence of exhaustive security correctness or production readiness.

---

## Additional Verification

### Runtime Failure Verification

Failure tests exercise:

- Post-commit response loss and idempotent retry.
- Connection-pool exhaustion.
- Worker termination before and after commit.
- PostgreSQL deadlock recovery (`40P01`).
- Canonical history and command-receipt recovery.

These scenarios verify persisted financial state, history, receipts, and selected provider checkpoints.

They do not reproduce PostgreSQL server crashes, complete infrastructure outages, or real reverse-proxy network failures.

### Async Execution Verification

The application's Financial Core primarily uses **synchronous SQLAlchemy**.

Separate PostgreSQL-backed experiments evaluate `AsyncSession.run_sync()` lifecycle and test-only native-async corrections under cancellation, response loss, conflicting writes, lock contention, and connection-pool recovery.

The native-async experiment passed **5/5 tests**.

It is not a production application rewrite or a native-async production correction endpoint.

ASGI middleware tests also cover cancellation, request-context isolation, and process-local metrics.

### Operational Verification

Exercises cover PostgreSQL backup/restore, projection rebuild, canonical audit, deployment preflight, readiness checks, SIGTERM shutdown, and incompatible schema revision detection.

There has been no production-traffic, availability, or real incident-response validation.

---

## Moneta Demo UI

Moneta is a lightweight **HTML/CSS/JavaScript interface** served by FastAPI at `GET /demo`.

It is a presentation layer over existing APIs, **not the core engineering deliverable**.

| Screen | Data source | V1 boundary |
| --- | --- | --- |
| Login | Keycloak OIDC Authorization Code + PKCE | Legacy password login is unavailable through Kong |
| Dashboard | `GET /summary` | Current API summary |
| Accounts | `GET /accounts`, `GET /projections/accounts` | Missing projections shown explicitly |
| Transactions | `GET /transactions` | Read-only; first 100 oldest-by-ID results |
| Reconciliation | `GET /reconciliations/{id}` | Lookup by ID, no listing endpoint |
| Engineering | Six static cards | Not a live operations console |

The Engineering cards cover canonical financial state, idempotency and concurrency, append-only history, reconciliation, API security and authorization, and reliability verification.

Browser JWTs are held only in memory. Logout clears displayed personal data.

API errors produce visible error or empty states rather than fabricated fallback data.

The UI does not initiate financial writes in v1.

**Implementation references:**

- `backend/static/moneta.html`
- `backend/tests/test_moneta_demo.py`

---

## Technology Stack

| Layer | Technology |
| --- | --- |
| Language | Python |
| API framework | FastAPI, Pydantic |
| ORM | SQLAlchemy |
| Primary database | PostgreSQL |
| Migrations | Alembic |
| Optional cache | Redis |
| Gateway experiment | Kong |
| Identity experiment | Keycloak |
| Tests | pytest |
| Demo UI | HTML, CSS, JavaScript |

Financial services primarily use synchronous SQLAlchemy. Native-async behavior is investigated separately.

---

## Running Locally

### Prerequisites

Python/pip and PostgreSQL are required.

Redis is optional at runtime and required for cache integration tests.

Use separate development and disposable test databases. Some tests require database-creation or failure-injection privileges.

### Install Dependencies

From the `backend` directory:

```bash
pip install -r requirements.txt
pip install -r requirements-test.txt
```

### Configure PostgreSQL

Set `DATABASE_URL` to a database intended for the current operation.

Example:

```bash
export DATABASE_URL="postgresql://USER:PASSWORD@localhost:5432/finance_dev_db"
```

Use a separate disposable database for tests.

### Apply Migrations

```bash
alembic upgrade head
```

Migrations targeting the protected `finance_db` database require explicit authorization, as documented in `backend/DEPLOYMENT_V1.md`.

Do not bypass database-safety checks when preparing a test environment.

### Run Tests

From the `backend` directory:

```bash
export DATABASE_URL="postgresql://USER:PASSWORD@localhost:5432/finance_test_db"
python -m pytest -q
```

For Redis integration tests, use a dedicated disposable Redis database:

```bash
export REDIS_TEST_URL="redis://127.0.0.1:6379/15"
export REQUIRE_REDIS_TESTS=1

python -m pytest -q tests/test_redis_fault_injection_correctness.py
```

Redis test fixtures may flush the configured test database.

**Warning:** Tests may create, modify, or destroy data. Never point the test runner at a database containing important records.

### Start FastAPI

```bash
uvicorn app.main:app --reload --port 8765
```

This starts standalone FastAPI only.

It does not automatically start Kong or Keycloak.

In standalone mode, the local email/password authentication flow can be used through:

- `POST /auth/register`
- `POST /auth/login`

Moneta also supports Keycloak OIDC login for local development when Keycloak is running separately.

Visit:

- Moneta: `http://127.0.0.1:8765/demo`
- Swagger: `http://127.0.0.1:8765/docs`

### Optional Kong Gateway Labs

The two Gateway Labs use different Compose files.

**HTTP Infrastructure Lab**

```bash
docker compose -f gateway/compose.http.yml up -d --build
```

**External Authorization Lab**

```bash
docker compose -f gateway/compose.internal.yml up -d --build
```

Both configurations bind Kong to `127.0.0.1:8004` and must **not** run concurrently.

The API container is not directly published to a host port in these configurations.

The Gateway Labs require their documented database, Keycloak, network, and authentication configuration prerequisites.

For integrated Kong + Keycloak operation, clients authenticate using Keycloak OIDC.

Legacy password login and registration routes are unavailable through Kong.

See [`gateway/README.md`](gateway/README.md) for complete setup and security boundaries.

---

## Scope and Limitations

Personal Finance Platform is an engineering case study, not a production-deployed financial product.

The current work does not establish:

- Exhaustive coverage of concurrent transaction interleavings.
- Correctness under every PostgreSQL isolation level.
- Complete resilience against infrastructure or network outages.
- Protection against a deliberately compromised Redis server.
- Zero-downtime schema migration compatibility.
- A fully native-async production application.
- A production distributed event-processing platform.
- Organization-wide authorization infrastructure.
- Complete OAuth2/OIDC infrastructure.
- Production operation of the Kong/Keycloak integration.
- Automatic Kong JWKS rotation or zero-downtime signing-key rotation.
- Production-grade performance, availability, or operational readiness.
- Reliable cache hit rates under unknown real-world workloads.
- Stable offset pagination under concurrent writes.
- Pagination for extremely large provider transaction histories.
- Performance improvements for every PostgreSQL workload.
- Complete financial write and reconciliation workflows through Moneta.

Some guarantees depend on application write paths consistently following the shared locking and transaction conventions.

Direct database writes or future code paths that bypass those conventions require separate verification.

The repository emphasizes **implemented behavior, reproducible tests, engineering decisions, and explicit limitations**.

In particular, surviving selected Redis failure scenarios does not prove that Redis can never contribute to a financial correctness defect.

**Development disclosure:** GitHub Copilot assisted with portions of implementation, test creation, and documentation.
