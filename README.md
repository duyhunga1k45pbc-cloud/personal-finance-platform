# Personal Finance Platform

**A correctness-focused financial backend engineering case study built with FastAPI, PostgreSQL, and SQLAlchemy.**

Personal Finance Platform (PFP) started as a conventional income/expense CRUD application. It evolved into a modular-monolith backend for investigating a harder question:

> How do financial operations preserve their intended meaning when requests are retried, commands race, transactions roll back, provider evidence conflicts, or infrastructure fails?

The project prioritizes **business invariants, transaction boundaries, idempotency, authorization, recoverability, and measured engineering decisions** over feature count. The repository includes a lightweight **Moneta** web UI for exploring existing APIs; the backend and its verification evidence remain the primary focus.

**Status:** Engineering portfolio and experimental application. **Not production-deployed or production-validated.**

## At a Glance

| Area | Implementation / evidence |
| --- | --- |
| Financial state | Canonical financial events and signed entries; income, expense, transfer, refund, reversal, and correction semantics |
| Correctness | Explicit invariants, PostgreSQL transactions, row locks, expected-version checks, and deterministic concurrency tests |
| Idempotency | Database-backed command receipts, conflicting-key handling, and recovery after an ambiguous response |
| History and recovery | Append-only financial history, causal links, canonical audits, rebuildable projections, and backup/restore exercises |
| External evidence | Provider ingestion and reconciliation that distinguish observations from accepted canonical financial state |
| Authorization | JWT verification, declared per-route policies, SQL ownership scoping, cross-user tests, and short-lived authentication sessions |
| Read performance | Removal of a provider-listing N+1 query pattern; optional Redis cache with PostgreSQL-backed committed revisions |
| Runtime resource management | Root-cause analysis and correction of connection-pool starvation under concurrent requests |
| Engineering decisions | Controlled sync/async A/B tests, CPU profiling, and explicit decisions to defer unproven optimizations |
| Demo | Moneta, a small HTML/CSS/JavaScript interface backed by existing authenticated FastAPI endpoints |

**Recent verification evidence:** The Moneta feature worktree reported **289 passed, 0 failed, 0 skipped** on a migrated PostgreSQL test database with real Redis integration tests enabled. Separate earlier integration experiments reported larger totals when additional *local-only research tests* were overlaid; those counts are not directly comparable. **The post-merge `main` CI result should be checked separately rather than inferred from local runs.** Passing tests establish behavior only under the scenarios exercised.

## Engineering Case Studies

### 1. Preventing Concurrent Over-Refunds

**Invariant:** The sum of active refunds against an original expense cannot exceed the amount eligible for refund.

For an expense of **100**, two concurrent refund requests of **60** must not both succeed.

**Reproduced failure:** Under concurrent PostgreSQL transactions, both requests could independently read the remaining refundable amount as 100, validate successfully, insert their own causal records, and commit. Each transaction was atomic, yet the final active refund total was **120**. Sequential validation and ordinary command idempotency did not protect this cross-request aggregate invariant.

**Correction:** Refund and reversal commands acquire a PostgreSQL `SELECT ... FOR UPDATE` lock on their shared original financial event *before* checking eligibility and writing causal state. With the tested PostgreSQL `READ COMMITTED` behavior, a competing command waits, then reevaluates against the committed result.

**Important interaction:** Concurrent retries of the *same* command must still recover the already committed response. After a losing attempt encounters changed eligibility, the implementation rechecks the command receipt so idempotent replay is not mistakenly treated as a new invalid refund.

**Evidence:** PostgreSQL-backed tests reproduce the vulnerable interleaving, observe lock contention, reject the over-refund, preserve the aggregate invariant, and verify same-key replay and competing refund/reversal behavior.

**Code:** `backend/app/causal_service.py`, `backend/app/canonical_service.py`, `backend/app/routers/causal_events.py`, and `backend/tests/test_refund_concurrency.py`.

**Boundary:** This proves a defined locking convention under tested conditions; it is not a claim that every possible concurrency defect is eliminated.

### 2. Correct Read Caching Without Making Redis the Source of Truth

The provider external-transaction listing had a measurable read-side cost: SQLAlchemy entity materialization, JSON encoding, and an earlier N+1 query pattern. An isolated Redis prototype improved throughput in high-hit workloads, but caching a financial response requires more than a TTL.

The implemented cache uses a **committed-data revision per provider connection**, stored in PostgreSQL:

```text
Provider evidence/transaction INSERT
        |
        v
PostgreSQL transaction
  +-- Insert business/evidence rows
  +-- Database trigger advances listing revision
  +-- COMMIT (or ROLLBACK both together)
        |
        v
Subsequent authorized GET
  +-- Authenticate and verify connection ownership in PostgreSQL
  +-- Read committed listing revision
  +-- Redis GET by user + connection + query + schema + revision
        |                          |
       HIT                        MISS
        |                          |
  Return cached bytes     Query PostgreSQL, encode once,
                          cache response with bounded TTL
```

The revision advances inside the writer's **same database transaction**, avoiding the crash window in a `COMMIT`-then-`Redis DEL` design. A reader holding an older revision can populate only an older revision key; later requests consult the newer one. Redis failures fall back to PostgreSQL. **Ownership checks occur before Redis access**, and the cache is disabled unless `REDIS_URL` is set.

**Evidence:** Tests cover commit/rollback and savepoint behavior, cross-user isolation, concurrent writers, stale in-flight population, Redis outages, and cache corruption. In an earlier local, synthetic experiment (20 users, 300 transactions per connection, six concurrent clients), read-only throughput increased from **144 to 271 RPS** and mixed read/write throughput from **126 to 198 RPS**. These are not production throughput guarantees and depend strongly on workload hit rate.

**Trade-offs:** Each cached request still performs authentication/ownership and revision lookups in PostgreSQL; cache misses add a query. Old revision keys remain until TTL expiry, causing memory churn under frequent writes. Hot-connection writers can contend on the revision row. The list endpoint is currently unbounded server-side.

**Code:** `backend/app/listing_cache.py`, `backend/alembic/versions/k1b9c6d3f477_provider_listing_revision.py`, `backend/tests/test_provider_listing_cache.py`, `backend/tests/test_provider_listing_revision_concurrency.py`.

### 3. Eliminating Connection-Pool Starvation Without Coupling Transactions

During cache benchmarking, the provider routes stalled at higher concurrency even though PostgreSQL was doing little active SQL work. Instrumentation identified **two simultaneously held database sessions/connections per request**: one for authentication and another for the business handler. With a 15-connection pool, concurrent requests could hold their authentication connections while waiting for business connections, exhausting the pool.

Two fixes were compared:

| Design | Outcome | Trade-off |
| --- | --- | --- |
| Independent, overlapping auth and handler sessions | Reproduced pool starvation | Separate transactions but concurrent connection ownership |
| Shared session for auth and handler | Removed observed starvation | Couples transaction boundaries; `rollback()` can expire the ORM `User` and trigger refresh queries |
| **Short-lived auth + immutable Principal** | **Removed observed starvation** | Retains two sequential session checkouts, but keeps transactions separate |

The selected design validates the JWT, looks up the user using a short-lived session, copies the required identity attributes into an immutable `Principal`, **returns the auth connection**, and then lets the business handler open its own transaction. Route authorization consumes the verified principal rather than depending on a live ORM `User` instance.

**Evidence:** Lifecycle instrumentation confirmed checkout → authentication query → checkin before handler checkout. Tests exercised a one-connection pool, authorization errors, handler exceptions, rollbacks, and concurrent financial commands. In representative local runs up to **32 concurrent clients**, the converted routes had **zero pool timeouts**; this does not establish an unlimited concurrency capacity or production service-level objective.

**Code:** `backend/app/routers/auth.py`, `backend/tests/test_auth_session_lifecycle.py` and protected routers using `get_principal` / `authorize_owner_principal`.

### 4. Measuring Instead of Assuming Async or Serialization Gains

The production-oriented financial services remain primarily synchronous. Native-async financial operations and `AsyncSession.run_sync()` have separate experimental tests for cancellation, rollback, retries, and transaction ownership. A controlled sync-versus-async listing comparison did **not** show a consistent end-to-end throughput advantage for switching that endpoint to async, so the synchronous implementation was retained.

Profiling of the original uncached provider listing attributed approximately **32.55% of GIL leaf samples** to SQLAlchemy materialization, **29.25%** to `jsonable_encoder`, and **4.45%** to JSON encoding. These are **sample shares, not percentages of total CPU time** or predicted savings.

A read-only audit found that the listing constructs full ORM objects when it needs only a subset of columns. Column projections and direct serialization could improve cache misses, but no validated post-cache benefit justified the extra change. **Decision: defer ORM/serialization optimization**, preserve response compatibility, and measure again only if the workload requires it.

## System Design

PFP is a **modular monolith**, not a distributed event-processing platform. PostgreSQL stores authoritative financial records. Redis is an optional, derived-response cache, not canonical state. Moneta calls existing HTTP endpoints; it does not bypass backend authorization.

```text
                  Moneta UI / API Clients
                            |
                            v
                FastAPI + Route Policies
                            |
               JWT -> Immutable Principal
                            |
                            v
                    Business Services
                            |
                     DB Transaction
                            |
             +--------------+---------------+
             |                              |
             v                              v
      Canonical Events                 Command Receipts
      Signed Entries                   Idempotency
      Causal Links
             |
      +------+-------+
      |              |
      v              v
 Append-only      Rebuildable
   History        Projections

 Provider Observations -> Normalization -> Interpretation / Reconciliation
                                   (not automatically canonical truth)

 Provider Listing Reads -> PostgreSQL Ownership + Revision -> Optional Redis
```

The responsibilities are deliberately separated:

- **Canonical financial state:** accepted financial events, signed entries, and causal relationships.
- **Historical integrity:** append-only evidence of interpretation and state evolution.
- **External evidence:** provider observations that can be delayed, duplicated, incomplete, or contradictory.
- **Reconciliation:** comparison of observed evidence and accepted state, surfacing disagreements rather than silently overwriting canonical records.
- **Projections:** rebuildable balances and summaries; a missing projection is reported explicitly.
- **HTTP/security:** JWT authentication, ownership-scoped SQL and declared route policies.
- **Operational mechanisms:** migrations, request observability, failure injection, readiness checks, and recovery exercises.

## Security Model

Authentication and authorization are separate. A valid JWT establishes a user identity; it does **not** grant access to another user's resources.

- The authenticated subject is represented by a **short-lived-session `Principal`**, not a long-lived attached ORM user.
- `ROUTE_POLICIES` declares each HTTP operation as `PUBLIC`, `OWNER`, or `METRICS_TOKEN`.
- An app-level dependency and startup route inventory reject undeclared HTTP routes; owner-scoped routers also enforce their declared policy.
- `owned(query, column, subject)` applies ownership filtering in SQL. Selected object reads additionally use `authorize_object`, bound to the current route's policy.
- Tests cover cross-user access, nested resources, policy drift, and preservation of existing HTTP status contracts (including 401, 404, and relevant 422 responses).

**Scope:** HTTP `APIRoute` handlers only. Mounts, raw routes, and WebSockets are not implicitly protected by this mechanism and require explicit review. Many service-layer queries retain their own ownership predicates; the policy layer does not replace them. This is not a complete OAuth2/OIDC identity platform.

See `backend/SECURITY_V1.md` and `backend/tests/test_authorization_policy.py`.

## Moneta Demo UI

Moneta is a **small, dependency-light HTML/CSS/JavaScript demo**, served by FastAPI at **`GET /demo`**. The page itself is public; protected API requests still require JWT authentication.

| Screen | Backend integration | Current boundary |
| --- | --- | --- |
| Login | `POST /auth/login` | Existing user credentials required; no built-in demo account |
| Dashboard | `GET /summary` | Shows API summary, not a claim that every canonical projection is populated |
| Accounts | `GET /accounts` and `GET /projections/accounts` | Balances may be unavailable until a projection exists |
| Transactions | `GET /transactions` | Read-only UI; at most 100 transactions currently fetched |
| Reconciliation | `GET /reconciliations/{id}` | Lookup by ID only; no listing API |
| Engineering | Static explanatory content | Not a live system diagnostics dashboard |

The browser holds the token **in memory only** (not in `localStorage`, `sessionStorage`, or cookies). Logout removes visible user data. The UI has loading/empty/error states and does **not** invent fallback financial balances when API calls fail. There is no UI transaction-creation flow in v1.

**Implementation:** `backend/static/moneta.html`, `backend/tests/test_moneta_demo.py`.

## Verification Strategy

Tests are written as evidence for specific guarantees, not as proof of universal correctness.

1. State an invariant or expected behavior.
2. Identify a concrete failure boundary (race, retry, crash, rollback, authorization bypass, cache staleness).
3. Reproduce it when possible using **real PostgreSQL**, not only mocks.
4. Implement the smallest mechanism that protects the boundary.
5. Run deterministic or adversarial regression tests.
6. Record what was actually exercised and what remains unproven.

Coverage spans financial semantics, causal concurrency, transactional idempotency, provider evidence, reconciliation, projection rebuilds, authorization, Redis revisions, connection lifecycle, schema evolution, and selected recovery/fault-injection scenarios.

**Evidence and limitations:**

- Recent local Moneta verification: **289 passed, 0 failed, 0 skipped**, including real Redis cache tests. This reflects that feature worktree; check current GitHub Actions on `main` for the post-merge outcome.
- Some earlier reports included *local-only* experimental tests that are not part of the ordinary committed suite, so test totals should not be compared without checking collection scope.
- Controlled experiments reproduced over-refunds, cache staleness when revision triggers were deliberately disabled, and pool starvation with overlapping auth sessions.
- Tests have covered selected worker termination, ambiguous commit acknowledgement, PostgreSQL deadlock (`40P01`), schema migration, and backup/restore scenarios. They do not establish real network-partition or PostgreSQL-server-crash behavior.
- The observed measurements are from local/synthetic workloads with bounded concurrency; production workload distributions and cache hit rates are unknown.

## Technology Stack

| Layer | Technology |
| --- | --- |
| Language / HTTP | Python, FastAPI, Uvicorn |
| Validation | Pydantic |
| Persistence | PostgreSQL, SQLAlchemy 2.0 |
| Migrations | Alembic |
| Optional response cache | Redis |
| Authentication / authorization | JWT, route policies, immutable Principal |
| Tests | pytest, real PostgreSQL and Redis integration tests |
| Async research | asyncio, SQLAlchemy AsyncSession, asyncpg |
| CI | GitHub Actions |
| Demo | Plain HTML, CSS and JavaScript (Moneta) |

## Running Locally

**Prerequisites:** Python, PostgreSQL, a dedicated development database, and a **separate disposable test database**. Redis is optional for the application, but required to exercise cache integration tests.

From `backend/`:

```bash
python -m pip install -r requirements.txt
python -m pip install -r requirements-test.txt

# Use a development database, never a production or important database.
export DATABASE_URL="postgresql://USER:PASSWORD@localhost:5432/finance_dev_db"
alembic upgrade head
uvicorn app.main:app --reload --port 8765
```

Open:

- Moneta UI: <http://127.0.0.1:8765/demo>
- Interactive API docs: <http://127.0.0.1:8765/docs>

Create a local user through `POST /auth/register` in the API docs and log in to Moneta. A new user may have no transactions; account-balance projections may require a separate rebuild before balances are available.

**Optional Redis cache:** Set `REDIS_URL` to a Redis endpoint to enable the provider-listing cache. Without it, the database-only request path remains active. The current cache uses PostgreSQL revisions; simply enabling Redis does not eliminate ownership or revision queries.

**Running tests:** Point `DATABASE_URL` to a separate disposable PostgreSQL database and run migrations first. Cache integration tests use a dedicated disposable Redis database configured with `REDIS_TEST_URL`; they call `FLUSHDB` and **must not** target shared Redis data.

```bash
export DATABASE_URL="postgresql://USER:PASSWORD@localhost:5432/finance_test_db"
export REDIS_TEST_URL="redis://localhost:6379/15"
export REQUIRE_REDIS_TESTS=1
alembic upgrade head
pytest -q
```

Some fault-injection or migration tests may require special database privileges or isolated test configurations. Consult `backend/DEPLOYMENT_V1.md`. Never run destructive tests against important records.

## Engineering Principles

- **Preserve business meaning.** Financial operations are not interchangeable rows; causal relationships and invariants matter.
- **Define transaction ownership.** Atomic commits alone do not guarantee correctness across concurrent transactions.
- **Treat retries as normal.** A command must not apply its financial effect twice because the first response was lost.
- **Separate observation from authority.** External evidence, canonical records, history, and derived projections have different roles.
- **Check authorization before cache access.** Cached data does not replace user ownership verification.
- **Measure before optimizing.** Keep sync/async, ORM, serialization, and caching decisions evidence-based.
- **Release shared resources deliberately.** Session lifecycle and connection ownership are correctness and availability concerns.
- **State the limits of evidence.** Passing tests establish behavior under tested conditions, not every possible execution path.
- **Control complexity.** Add mechanisms for explicit failure boundaries rather than to maximize the technology list.

## Scope and Known Limitations

PFP is a **portfolio engineering case study**, not a production financial product. In particular, it does **not** establish:

- Production-scale traffic, availability, incident-response, or on-call reliability.
- Exhaustive concurrency interleavings or correctness under every PostgreSQL isolation level.
- Complete resilience to external outages, network partitions, or database-server crashes.
- A production OAuth2/OIDC identity provider or multi-service authorization platform.
- Zero-downtime migrations or a fully native-async production backend.
- A distributed financial event-processing architecture.
- Efficient deep-history browsing: the provider external-transaction listing is currently **unbounded**, and may need cursor pagination and query/index review as data grows.
- Full UI coverage of backend commands: Moneta is primarily read-only and intentionally does not initiate financial mutations.

Some invariants also rely on supported application write paths observing documented locking and transaction conventions; direct database writes and future integrations require separate review.

---

## Portfolio Note

PFP is intended to demonstrate backend engineering through **concrete failure scenarios, measured trade-offs, and reproducible verification evidence**, not claims about production scale.

**Business Requirement → Invariant → Failure Boundary → Design Decision → Implementation → Test Evidence → Known Limitation**
