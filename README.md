# Personal Finance Platform

**A correctness-focused, experimental financial platform built around a FastAPI Financial Core, PostgreSQL, and SQLAlchemy.**

This project explores how financial state can remain consistent and recoverable when requests retry, concurrent operations conflict, transactions fail, external evidence disagrees, or execution conditions change.

The Financial Core implements financial event modeling, concurrency control, transactional idempotency, reconciliation, recovery tooling, resource-scoped RBAC/ABAC authorization, a durable activity feed with shared SSE delivery, and optional Redis caching. The integrated PFP environment also includes a Go AI Analysis Service, a React/TypeScript frontend, Keycloak authentication, and Kong API gateway integration. Automated unit and integration suites are complemented by real browser E2E verification across the integrated flow.

**Project status:** Engineering portfolio and experimental application. Not production-deployed.

**Quick navigation:** [Over-refund](https://github.com/duyhunga1k45pbc-cloud/personal-finance-platform#case-study-preventing-concurrent-over-refunds) · [Redis revision cache](https://github.com/duyhunga1k45pbc-cloud/personal-finance-platform#case-study-transaction-consistent-redis-caching) · [Connection pool RCA](https://github.com/duyhunga1k45pbc-cloud/personal-finance-platform#case-study-connection-pool-starvation) · [Index & pagination](https://github.com/duyhunga1k45pbc-cloud/personal-finance-platform#case-study-deterministic-pagination-and-postgresql-indexing) · [Async lifecycle](https://github.com/duyhunga1k45pbc-cloud/personal-finance-platform#case-study-async-sqlalchemy-event-loop-lifecycle-isolation) · [Snapshot-bound evidence](#case-study-snapshot-bound-evidence-validation) · [Provider concurrency](#case-study-bounded-provider-concurrency-and-cancellation) · [Reproducible Insights E2E](#case-study-from-integration-drift-to-reproducible-e2e) · [Architecture](https://github.com/duyhunga1k45pbc-cloud/personal-finance-platform#system-structure) · [Security](https://github.com/duyhunga1k45pbc-cloud/personal-finance-platform#security-boundaries) · [Authorization E2E](https://github.com/duyhunga1k45pbc-cloud/personal-finance-platform#api-authorization-invariants-end-to-end-verification) · [Verification](https://github.com/duyhunga1k45pbc-cloud/personal-finance-platform#test-results-and-coverage)

---

## Engineering Highlights

| **Area**                  | **Engineering evidence**                                                           |
| ------------------------- | ---------------------------------------------------------------------------------- |
| **Financial correctness** | PostgreSQL locking, idempotent commands, append-only history, reconciliation       |
| **Cache consistency**     | PostgreSQL-revisioned Redis keys, ownership-before-cache checks, fault injection   |
| **Query behavior**        | Deterministic pagination and measured composite-index optimization                 |
| **Reliability**           | Connection-pool lifecycle, async resource isolation, recovery and migration checks |
| **Security**              | Resource-scoped RBAC/ABAC, grant/revoke E2E, Keycloak/Kong integration       |
| **Event delivery**        | Durable activity feed, shared SSE fan-out, cursor replay and shutdown tests        |
| **Verification**          | PostgreSQL/Redis fault injection, concurrency, integration, and real browser E2E |
| **Demo**                  | Moneta read-only interface backed by real financial APIs                           |

**Historical local backend suite:** **347 passed, 0 failed, 0 skipped in 136.83 seconds**, using PostgreSQL-backed tests and real Redis integration.

**Historical GitHub Actions backend suite (October 19, 2026):** **359 passed, 0 failed, 537 warnings in 205.07 seconds**, as shown in the pytest CI output. The warnings are reported separately from test failures; their individual causes require separate review.

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

```
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

| **Workload**             | **Before Redis cache** | **After Redis cache** |
| ------------------------ | ---------------------- | --------------------- |
| Read-only traffic        | 144 RPS                | 271 RPS               |
| Mixed read/write traffic | 126 RPS                | 198 RPS               |

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

| **Approach**                                         | **Observed result**                           | **Trade-off**                                            |
| ---------------------------------------------------- | --------------------------------------------- | -------------------------------------------------------- |
| Overlapping authentication and business sessions     | Pool starvation under tested load             | Unnecessary simultaneous connections                     |
| Single shared session                                | Removed observed pool exhaustion              | Authentication and business share transaction lifecycle  |
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

## Case Study: Durable Activity Feed and Shared SSE Lifecycle

Financial transactions require a durable record of activity before connected clients can reliably receive updates. The activity feed is persisted in PostgreSQL alongside committed financial changes; SSE is a delivery mechanism rather than the source of truth.

### Correctness and Scaling Boundary

The shared-stream implementation uses process-local polling and subscriber fan-out to avoid a separate database polling loop for every connected client. Database-backed cursors support replay after reconnect. Verified scenarios include concurrent event ordering, subscription handoff, user isolation, bounded subscriber queues, and poller shutdown.

### Verification and Limitations

PostgreSQL-backed tests exercise cursor replay, subscriber isolation, subscription handoff, bounded queues, poller failure, and shutdown. The shared poller reduces redundant database polling within a process; it is not a cross-instance event bus.

The separate async lifecycle case study below documents the integration failure discovered during full-suite verification. Passing CI checks do not establish production SSE availability or multi-instance delivery guarantees.

**Implementation references:**

- `backend/app/activity_feed_reader.py`
- `backend/app/routers/activity_shared_stream.py`
- `backend/tests/test_activity_stream.py`
- `backend/tests/test_activity_shared_stream.py`
- `backend/tests/test_activity_stream_lifecycle.py`

---

## Case Study: Async SQLAlchemy Event-Loop Lifecycle Isolation

### The Symptom

A Python 3.12 full backend CI run failed on the SSE future-cursor validation test with `Future attached to a different loop`. The focused SSE tests had passed, making the failure dependent on broader test execution context.

### Root Cause and Ownership Boundary

The SSE reader uses a module-level SQLAlchemy `AsyncEngine` with pooled asyncpg connections. A direct reader test called the shared engine inside `asyncio.run()`, while SSE HTTP tests used a module-level `TestClient` without entering application lifespan. These patterns could leave pooled connections associated with an earlier event loop and subsequently reuse them from another loop.

The observed exception was consistent with cross-loop connection reuse. The audit identified this unsafe ownership path, although the exact connection checkout sequence in the failing CI run was not traced.

Closing an `AsyncSession` returns a connection to its pool; it does not necessarily dispose that connection. Resource lifetime must therefore be managed at the engine and event-loop boundaries.

### The Fix

- SSE HTTP tests now use lifespan-managed `TestClient` fixtures, with requests sharing a client event loop and existing shutdown cleanup disposing the async engine.
- The account-sharing test uses a test-owned pooled async engine and disposes it before its `asyncio.run()` event loop closes.
- Regression assertions check event-loop identity within a TestClient lifespan and distinct loops with pool cleanup across successive lifespans.
- A further full-suite failure exposed process-local deployment state remaining `DRAINING` after SSE test shutdown. An autouse fixture restores test state before and after each SSE test.

The changes are confined to test isolation. Production connection pooling, fail-closed startup checks, SSE replay behavior, and financial write semantics remain unchanged. The fix does not use permanent `NullPool` or bypass startup readiness.

### Verification Evidence

The original Python 3.12 CI run reported **355 passed, 1 failed** on the cross-event-loop exception. After the first isolation patch, a later run reported **357 passed, 1 failed**: SSE tests passed, but an unrelated commit-response-loss test could not start because deployment state had leaked from SSE shutdown. The final state-isolation patch passed **7/7 targeted tests** locally. A subsequent October 19 GitHub Actions backend run completed with **359 passed, 537 warnings in 205.07 seconds**, confirmed by the pytest summary shown in the supplied CI screenshot.

These results support the test-lifecycle correction in the evaluated configurations. They are not evidence that every async resource-lifecycle failure or production incident mode has been eliminated.

**Implementation references:**

- `backend/app/activity_feed_reader.py`
- `backend/app/main.py`
- `backend/tests/test_activity_stream.py`
- `backend/tests/test_activity_stream_lifecycle.py`
- `backend/tests/test_account_sharing.py`

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

```
SELECT ...
FROM transactions
JOIN financial_events ON ...
WHERE transactions.user_id = :user_id
ORDER BY transactions.id ASC
LIMIT :limit OFFSET :skip;

```

The audit evaluated a composite B-tree index:

```
CREATE INDEX CONCURRENTLY
ON transactions (user_id, id);

```

The index was introduced through Alembic migration `l2c0d7e4a588`.

The migration handles an interrupted invalid index build and was tested through upgrade, downgrade, and repeat upgrade on a disposable database.

### Measurement and Trade-offs

Selected `EXPLAIN (ANALYZE, BUFFERS)` results from a synthetic PostgreSQL audit database containing approximately **498,000 transactions**:

Median of nine warm-cache runs.

| **Request/query component**                     | **Before index** | **After index** |
| ----------------------------------------------- | ---------------- | --------------- |
| User with \~100 transactions, first page        | 21.93 ms         | 0.02 ms         |
| User with \~10,000 transactions, first page     | 27.24 ms         | 19.24 ms        |
| User with \~10,000 transactions, offset 5,000   | 36.77 ms         | 20.80 ms        |
| User with \~100,000 transactions, offset 50,000 | 17.66 ms         | 17.60 ms        |

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

## Case Study: Snapshot-Bound Evidence Validation

### Engineering Problem and Invariant

Syntactically valid structured output is not necessarily trustworthy. An analysis result can parse correctly while naming a metric that does not match its referenced fact, citing evidence from a different fact, or using evidence from another financial snapshot.

FastAPI remains the authority for financial calculations and ownership. It builds Financial Context v2 from the authenticated owner's fresh projection, including a generation-bound snapshot ID, typed facts, evidence IDs, and freshness metadata. Go validates the supplied context and constrains output to references into that exact context; it does not query or write the financial database.

The invariant is: **every returned finding resolves to an available fact and evidence record belonging to the same trusted snapshot.**

### Design Risk and Validation Boundary

Go rejects unsupported context and output versions, stale or internally inconsistent context, invalid metric facts, malformed identifiers, and evidence records that do not match their snapshot and fact. Model output is limited to 8 KiB and decoded with duplicate-key, unknown-field, and trailing-value rejection. Each finding must match its fact's metric, cite evidence for that fact in the same snapshot, and use a claim code allowed by the fact's availability. An empty findings array is valid.

This is a preventive engineering design supported by tests, not a production incident. Structural validation proves reference consistency with the trusted input; it does not prove that the input itself came from FastAPI or that an interpretation is semantically correct. The Go handler explicitly assumes trusted context provenance. FastAPI's authentication, owner scoping, and freshness checks remain essential to that boundary.

### Verification Evidence

Focused Go tests cover accepted empty output; deterministic, snapshot-bound fact and evidence IDs; unsupported versions; snapshot, fact, evidence, and metric mismatches; duplicate keys; unknown fields; trailing JSON; output size limits; claim-code availability; and duplicate findings. Separate tests cover deterministic result rendering and prevention of trusted-context mutation by the provider implementation.

**Implementation and test references:**

- [backend/app/analysis_context.py](backend/app/analysis_context.py)
- [backend/app/routers/analytics.py](backend/app/routers/analytics.py)
- [ai-service/internal/analysis/analysis.go](ai-service/internal/analysis/analysis.go)
- [ai-service/internal/analysis/model_output.schema.json](ai-service/internal/analysis/model_output.schema.json)
- [ai-service/internal/analysis/analysis_test.go](ai-service/internal/analysis/analysis_test.go)

### Trade-offs and Generalizable Lesson

Go emits typed claim references, while result descriptions and numeric values are rendered deterministically from FastAPI's context. This sacrifices open-ended model prose in exchange for a narrow, testable output boundary. Validation can reject unsupported references and prevent model-supplied numbers from becoming authoritative facts; it cannot certify the upstream projection or every semantic interpretation.

---

## Case Study: Bounded Provider Concurrency and Cancellation

### Engineering Problem and Invariant

External analysis providers can be slow, fail, or ignore cancellation. Letting every incoming request create provider work would make resource use grow with request volume. A queue would also make overload latency and cancellation less predictable.

The Go service admits at most ten provider calls per process. Admission is nonblocking: a request that finds all slots occupied receives a sanitized 503 instead of waiting in an unbounded queue.

### Request and Provider Lifecycle

The incoming request body is bounded separately from the 8 KiB model-output limit. After strict input validation, analysis has an eight-second deadline and the provider call a seven-second timeout. Request cancellation propagates through the provider interface's context.Context.

A provider slot is released only after Explain returns, including error and panic unwinding. A provider that ignores its context cannot be forcibly stopped; its slot remains occupied while it is still running. The HTTP server handles SIGINT/SIGTERM and drains active requests for a bounded period before force-closing connections.

### Verification Evidence and Limits

Go tests use blocking fakes to verify that ten calls are admitted, the eleventh is rejected promptly, and capacity is released after success, provider error, or panic. Cancellation propagation, timeout behavior, slot retention while a provider remains blocked, concurrent metrics snapshots, and graceful shutdown are also covered.

These tests establish limiter behavior under controlled fakes. They are not a 200-request load test with five-second real-provider latency and do not demonstrate a real LLM provider's cancellation behavior. The limit is per process, so ten slots on each of several replicas means a larger aggregate limit; there is no distributed/global limiter. Metrics are in-memory snapshots used by tests, not an externally exported monitoring endpoint.

**Implementation and test references:**

- [ai-service/internal/analysis/analysis.go](ai-service/internal/analysis/analysis.go)
- [ai-service/internal/analysis/analysis_test.go](ai-service/internal/analysis/analysis_test.go)
- [ai-service/cmd/server/main.go](ai-service/cmd/server/main.go)
- [ai-service/cmd/server/main_test.go](ai-service/cmd/server/main_test.go)

### Generalizable Lesson

Bound the lifetime of actual work, not only the lifetime of its HTTP request. Cancellation should reach the provider, while capacity should remain accounted for until provider work has truly ended.

---

## Case Study: From Integration Drift to Reproducible E2E

### Engineering Problem and Invariant

FastAPI, Go, PostgreSQL, Keycloak, Kong, and the React/Vite browser flow were developed and integrated before there was one isolated, reproducible full-system verification path. The invariant for the live test became: **a browser result is meaningful only if each hop uses the intended identity, API image, database, network, and financial snapshot.**

### Observed Failures and Systemic Diagnosis

The local integration history exposed several distinct mismatches:

- Live Keycloak issuer, redirect/PKCE and audience settings, and its active RS256 signing key did not initially match the local browser and Kong configuration. Login could not complete until the runtime client and Kong public-key credential matched live discovery/JWKS data.
- An existing FastAPI image expected a different Alembic revision from the intended database. Readiness reported the mismatch; a source-matched image reported the database's actual head.
- Kong returned 503 while the API DNS alias was unavailable during startup. Its logs showed name-resolution failures; starting Kong after API readiness resolved the observed failure. DNS negative caching is a plausible contributor, not a proven internal Kong cause.
- Early runs depended on pre-existing PostgreSQL port and Docker-network configuration. The initial port 5433 publication failed, while a WSL PostgreSQL listener on 5432 made an implicit localhost URL unsafe. The final automation uses a private PostgreSQL service inside its own Compose project rather than relying on either host binding.
- Early runner attempts exposed assumptions in JWKS hostname comparison, Docker build context, migration revision parsing, and the seed script's Python import path. These were runner defects, not financial-domain failures.
- pg_isready alone was insufficient to prove that the specific test database could be opened. The runner now waits for SELECT current_database(), current_user, verifies both values, and confirms the new database has no application tables before migration.
- Alembic revision parsing also needed to avoid accepting a revision as a partial match at a hyphen boundary. The validator now compares complete revision tokens; a regression case checks that abc does not satisfy a source head of abc-def. This was a false-positive risk in the matcher; the evidence does not show that an incorrect revision was actually accepted during a live run.

The common contributors were configuration drift between independently started components, stale deployment artifacts, readiness checks that proved process availability rather than dependency identity, and assumptions about inherited local state. The image/schema mismatch and runner errors are directly confirmed. The precise mechanism behind Kong DNS caching and the original port-5433 Docker Desktop failure was not established.

### Engineering Solution

The final runner creates a unique Compose project and disposable PostgreSQL database and volume for each invocation. It refuses protected or unscoped database names, verifies the effective FastAPI/Alembic URL, confirms database identity and emptiness, and checks the Alembic head. It builds API and Go images without cache from the current source.

The runner generates runtime-only credentials and Keycloak client configuration for the selected loopback origin. It checks the live issuer, PKCE S256, redirect URI, audience, and JWKS, then derives a Kong RS256 public-key credential from the active JWKS. It starts Go and FastAPI only after PostgreSQL identity and migration checks, verifies Go health, Keycloak JWKS reachability, and internal DNS against the run's container IPs, and starts Kong only after the upstream API is healthy.

Playwright exercises real Keycloak login and API traffic; it does not mock authentication or financial APIs. The browser scenario checks /auth/me, owner-scoped account and transaction operations, projection freshness, the empty-body Insights request, three deterministic findings with evidence IDs, matching snapshot metadata, logout clearing protected state, and missing/invalid JWT rejection. Cleanup is scoped to the runner's unique Compose project, its disposable volume, and run-specific images.

**Implementation and test references:**

- [gateway/e2e/run_live_e2e.py](gateway/e2e/run_live_e2e.py)
- [gateway/e2e/compose.live.override.yml](gateway/e2e/compose.live.override.yml)
- [gateway/e2e/test_run_live_e2e_unit.py](gateway/e2e/test_run_live_e2e_unit.py)
- [gateway/e2e/README.live.md](gateway/e2e/README.live.md)
- [frontend/e2e/live-financial-insights.spec.ts](frontend/e2e/live-financial-insights.spec.ts)
- [ai-service/internal/analysis/analysis.go](ai-service/internal/analysis/analysis.go)
- [backend/app/routers/analytics.py](backend/app/routers/analytics.py)

### Verification Evidence

Local verification first passed the real browser flow, then focused runner unit tests exposed an Alembic revision-matching bug. After correcting the matcher, the real browser E2E passed again. The focused runner unit suite reported **15 passed**; it exercises database-name and identity guards, Alembic revision matching (including the hyphenated-prefix regression), runtime Keycloak configuration, RS256 key extraction, readiness failure sanitization, and project-scoped cleanup behavior. These unit tests are not full Docker fault injection.

The later CI integration history and completed clean-checkout results are documented in [CI Integration and Regression Findings](#ci-integration-and-regression-findings) and [Automated System E2E on GitHub Actions](#automated-system-e2e-on-github-actions).

### Trade-offs and Limitations

The runner's cleanup uses project-scoped Compose teardown with --volumes, so it deletes the test run's own disposable database volume. Safety depends on its unique project/database checks; this is not a cleanup command for a developer's existing Compose project. The recorded runs used synthetic identities and data and did not touch finance_db, finance_test, or pfp_smoke_test.

The Go service still uses a deterministic development provider. There is no real LLM integration, no production reliability evidence, and no verification here of every hard-termination or infrastructure-failure path. Local working-tree results and committed-source CI results are separate: the CI evidence below applies to the exact PR commit in a clean checkout and does not validate any local uncommitted changes.

### CI Integration and Regression Findings

The integration work exposed source and commit consistency problems as well as application integration defects:

1. A local real browser E2E run passed through Keycloak, Kong, FastAPI, PostgreSQL, and the Go deterministic provider.
2. Focused runner unit tests passed and exposed the Alembic revision-matching bug at a hyphen boundary.
3. The real browser E2E passed again after the Alembic correction.
4. An early GitHub Actions run failed because the runner unit-test file was absent from the pushed commit; the local working tree and that commit did not contain the same files ([run](https://github.com/duyhunga1k45pbc-cloud/personal-finance-platform/actions/runs/45692600015)).
5. Backend CI then encountered an import mismatch: analytics tests imported lifecycle functions that were not present in the committed `app.main` ([run](https://github.com/duyhunga1k45pbc-cloud/personal-finance-platform/actions/runs/45692955692)).
6. A later backend CI run reported **368 passed and 15 failed**. The analytics routes returned HTTP 403 because the committed authorization policies were missing ([run](https://github.com/duyhunga1k45pbc-cloud/personal-finance-platform/actions/runs/45693687363)). Those 15 failures were outcomes of the shared authorization-policy problem, not 15 distinct bugs.
7. Browser E2E timed out while waiting for **Sign in with Keycloak** because the committed `App.tsx` still rendered the previous Login UI ([run](https://github.com/duyhunga1k45pbc-cloud/personal-finance-platform/actions/runs/45693687001)).
8. Local fixes were then verified with **22 targeted backend tests**, **5 frontend auth tests**, and **1 real browser E2E**. These local results are distinct from CI counts.
9. The final PR-head CI results are recorded below after verification of the actual completed workflow runs.

These events distinguish local working-tree correctness (what passed against local files), Git commit completeness (whether the needed files and fixes were included), CI clean-checkout reproducibility (whether that exact commit passed from a fresh checkout), and application integration correctness (whether the connected components behaved correctly together). The missing test file and uncommitted UI incidents were source-integration failures, not evidence of complex distributed-system race conditions.

### Generalizable Engineering Lesson

End-to-end reliability is a property of the complete runtime contract. Verify identity, schema, network reachability, service health, and data isolation explicitly; do not infer them from a process being up or a mocked request succeeding.

---

## System Structure

The **FastAPI Financial Core** follows a modular-monolith design.

The architecture keeps financial business rules, transaction boundaries, and authoritative state explicit without introducing distributed infrastructure unnecessary for the project's current scope. The SSE activity feed remains part of the modular monolith, with process-local shared polling rather than a distributed message broker.

The integrated PFP environment also runs the Go AI Analysis Service, React frontend, Keycloak, and Kong as separate components; these are not modules inside the Financial Core. Keycloak and Kong are optional when running the Financial Core standalone, and participate in the integrated authentication and gateway flow when enabled.

### Component Responsibilities

| **Component**             | **Responsibility**                                                                  |
| ------------------------- | ----------------------------------------------------------------------------------- |
| FastAPI Financial Core    | Owns financial correctness, authenticated user ownership, validation, and operations |
| PostgreSQL                | Stores authoritative financial state and persistent history                         |
| Go AI Analysis Service    | Consumes trusted Financial Context v2; validates output references; does not own financial facts |
| React/TypeScript frontend | Handles browser interaction and calls the platform APIs                             |
| Keycloak                  | Authenticates users in the integrated OIDC flow                                     |
| Kong                      | Routes API traffic and verifies JWTs in the relevant integration mode              |
| FastAPI routers           | HTTP handling, authentication dependencies, validation, responses                  |
| Business services         | Financial operations, invariants, causal rules, transaction coordination            |
| Provider integration      | Ingestion, normalization, reconciliation                                              |
| Derived projections       | Rebuildable balances and summaries                                                  |
| Operational mechanisms    | Idempotency, readiness, migrations, auditing, recovery                               |
| Activity delivery         | PostgreSQL activity feed, SSE replay, shared process-local subscribers               |

### Financial State Flow

```
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
- Account-sharing roles and grants with resource-scoped RBAC/ABAC checks.
- Ownership checks before joins, counts, pagination, and aggregation.
- PostgreSQL-backed authorization and cross-user tests.

**Implementation references:**

- `backend/app/authorization.py`
- `backend/app/routers/auth.py`
- [`backend/SECURITY_V1.md`](https://github.com/duyhunga1k45pbc-cloud/personal-finance-platform/blob/main/backend/SECURITY_V1.md)

### API Authorization Invariants: End-to-End Verification

Authorization correctness is not established by checking a single HTTP `200` or `403`. Access boundaries must remain enforced as resource-sharing permissions change, including when two users hold concurrent realtime connections.

The project adds reproducible multi-user end-to-end regressions for **FastAPI resource-scoped authorization**, distinct from the optional Kong external-authorization lab.

**Scenarios covered by the regression work:**

- **Unauthorized cross-user access:** A user without an account-sharing grant cannot read another user's protected account.
- **Grant lifecycle:** A `VIEWER` grant permits scoped reads without granting write authority.
- **Revocation:** Removing the grant removes the previously granted resource access in the evaluated request sequence.
- **Concurrent SSE isolation:** Simultaneously connected users receive user-scoped activity without cross-user event leakage in the tested scenario.
- **Reproducible identity setup:** The E2E fixture configuration includes repeatable multi-user identity seeding and Keycloak realm data.

**Evidence and implementation references:**

- `gateway/e2e/test_authorization_invariants_e2e.py`
- `gateway/e2e/seed_identity.py`
- `gateway/e2e/realm.json`
- Commit [`c22cc1b`](https://github.com/duyhunga1k45pbc-cloud/personal-finance-platform/commit/c22cc1b93515c53d67b27aee4510ee03384f3b8b): cross-user authorization and concurrent SSE isolation.
- Commit [`868b47b`](https://github.com/duyhunga1k45pbc-cloud/personal-finance-platform/commit/868b47bf99e0be8db9b1a36afd16f5a85eb7f1a2): reproducible authorization/SSE E2E setup.
- [PR #13](https://github.com/duyhunga1k45pbc-cloud/personal-finance-platform/pull/13), merged on October 19, 2026, via [`463a95f`](https://github.com/duyhunga1k45pbc-cloud/personal-finance-platform/commit/463a95f5df443b9574ff8e4804e338ffbc11ec06).

This is scenario-specific regression evidence, not proof of every revocation-versus-delivery interleaving, SSE reconnect race, or cross-instance stream isolation. The dedicated E2E tests are separate from the **359-test backend suite** and should not be added to that count without a verified combined run.

### Optional Gateway and Identity Labs

Kong and Keycloak are **optional integration experiments**, not prerequisites for Financial Core correctness.

The Gateway Lab has two explicit modes.

| **Capability**                                | **HTTP Infrastructure Lab** | **External Authorization Lab**          |
| --------------------------------------------- | --------------------------- | --------------------------------------- |
| Configuration                                 | `gateway/compose.http.yml`  | `gateway/compose.internal.yml`          |
| API routing                                   | Kong                        | Kong                                    |
| JWT verification                              | Kong                        | Kong                                    |
| Request correlation / infrastructure behavior | Enabled in HTTP Lab         | Separate pilot configuration            |
| External authorization                        | **Not used**                | Kong external-authorization integration |
| Financial resource ownership                  | FastAPI                     | FastAPI                                 |
| Financial domain invariants                   | Financial Core              | Financial Core                          |

**HTTP Infrastructure Lab**

Kong handles API routing, request correlation IDs, IP-based rate limiting, and JWT verification.

It does not call the external authorization endpoint. FastAPI retains route authorization and SQL resource ownership enforcement.

**External Authorization Lab**

Preserves the earlier Kong authorization pilot, including fail-closed behavior under selected failures and route-specific policy enforcement.

This is not an organization-wide authorization platform or reusable cross-service authorization framework.

Both modes proxy to the same FastAPI backend. Neither changes financial transactions or canonical domain invariants.

See [`gateway/README.md`](https://github.com/duyhunga1k45pbc-cloud/personal-finance-platform/blob/main/gateway/README.md) for setup, login-route scope, limitations, and signing-key rotation.

### Schema and Migration Safety

Alembic verification covers transactional rollback, historical backfill, incompatible-revision readiness checks, concurrent index creation, downgrade, and interrupted invalid-index recovery.

Test database safeguards reject the protected `finance_db` target. Production migrations require explicit authorization.

These mechanisms do not establish zero-downtime schema evolution.

---

## Test Results and Coverage

The repository includes unit, integration, concurrency, failure-injection, SSE lifecycle, authorization, and end-to-end tests.

Database-sensitive scenarios use PostgreSQL. Optional cache tests include real Redis integration.

### Local Test Results

| **Verification**                                   | **Reported result**                 |
| -------------------------------------------------- | ----------------------------------- |
| Backend test suite (historical local)             | **347 passed, 0 failed, 0 skipped** |
| Kong authorization integration suite               | **8 passed**                        |
| Gateway full-stack E2E                             | **2 passed**                        |
| Gateway Lab static configuration tests             | **2 passed**                        |
| Targeted backend authorization/security/OIDC tests | **71 passed**                       |
| Native-async correction experiment                 | **5 passed**                        |
| Backend CI (October 19; historical)                | **359 passed, 0 failed; 537 warnings** |

These are **separate test runs**. Their counts must not be summed into a single suite result.

The historical full backend result was obtained locally in 136.83 seconds using PostgreSQL-backed tests and real Redis. The **359 passed** figure comes from the October 19 GitHub Actions pytest output, not this historical local run.

**Historical Backend CI pytest result (October 19, 2026): 359 passed, 0 failed, 537 warnings in 205.07 seconds.** The warning total is not a failure count; the screenshot does not establish whether all warnings have the same underlying cause.

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

| **Commit** | **Workflow**                          | **Result** |
| ---------- | ------------------------------------- | ---------- |
| `4292cb2`  | Kong Authorization Integration Tests  | Passed     |
| `4292cb2`  | Gateway Full-Stack E2E                | Passed     |
| `4292cb2`  | Backend CI                            | Passed     |
| `27bb173`  | Gateway Full-Stack E2E                | Passed     |
| `27bb173`  | Backend CI                            | Passed     |
| `f767456`  | Backend CI (pull request)             | Passed     |
| `f767456`  | Gateway Full-Stack E2E (pull request) | Passed     |
| `f767456`  | Gateway Full-Stack E2E (push)         | Passed     |

The dedicated Kong integration workflow did not run for commit `27bb173`. The three successful `f767456` checks refer to an earlier PR screenshot; PR #13 was subsequently merged on October 19, and a later backend CI screenshot displays 359 passing tests.

On October 19, [PR #13](https://github.com/duyhunga1k45pbc-cloud/personal-finance-platform/pull/13) merged cross-user authorization and SSE-isolation E2E regression coverage into `main` (merge commit `463a95f`). The new authorization-invariant suite is distinct from the Kong integration workflow and the backend pytest count. The supplied CI screenshot shows a completed backend pytest run with 359 passing tests; it does not independently enumerate each dedicated E2E scenario.

The authorization integration tests cover timeout, connection failure, malformed response, and HTTP 500 behavior under the External Authorization Lab.

The E2E suite verifies authenticated reads, a denied write without database mutation, and parity between deployment and E2E Kong configurations under the tested conditions.

These results are not evidence of exhaustive security correctness or production readiness.

### Automated System E2E on GitHub Actions

PR #17, [“ci: add automated full-system E2E verification”](https://github.com/duyhunga1k45pbc-cloud/personal-finance-platform/pull/17), was merged. The following **completed pull-request runs** all checked out PR head `47dc6e2996be587fd9083c06d1a57701e46c0468` (short SHA `47dc6e2`):

| **Workflow** | **Verified result** |
| ------------ | ------------------- |
| [Backend CI](https://github.com/duyhunga1k45pbc-cloud/personal-finance-platform/actions/runs/45695988126) | **383 passed, 559 warnings** in the full backend regression suite. A separate Redis integration selection in the same workflow reported **17 passed**. |
| [Gateway Full-Stack E2E](https://github.com/duyhunga1k45pbc-cloud/personal-finance-platform/actions/runs/45695988145) | Workflow completed successfully. |
| [Kong Authorization Integration Tests](https://github.com/duyhunga1k45pbc-cloud/personal-finance-platform/actions/runs/45695988067) | **8 passed**; Docker cleanup verification reported PASS. |
| [System Browser E2E](https://github.com/duyhunga1k45pbc-cloud/personal-finance-platform/actions/runs/45695988097) | On a GitHub-hosted `ubuntu-24.04` runner, **15 runner unit tests passed**, the real Playwright browser E2E reported **1 passed**, and the step that runs E2E and verifies cleanup completed successfully. |

These test counts belong to separate selections and are not added together. The System Browser E2E used a disposable Compose project and database, ran the real Keycloak-to-Kong-to-FastAPI-to-Go browser flow, and verified cleanup. It does not establish real LLM behavior, production reliability, or coverage of every failure mode.

The successful rows above are PR-head results, not a claim that every workflow passed on merge commit `17900898f099b2b267b241d5de61322667317fc7`. At review time, the post-merge [Gateway Full-Stack E2E](https://github.com/duyhunga1k45pbc-cloud/personal-finance-platform/actions/runs/45696767134) and [Kong Authorization Integration](https://github.com/duyhunga1k45pbc-cloud/personal-finance-platform/actions/runs/45696767131) runs had passed, [Backend CI](https://github.com/duyhunga1k45pbc-cloud/personal-finance-platform/actions/runs/45696767154) was still in progress, and no System Browser E2E run for the merge SHA was present in the inspected run list. No complete merged-commit CI pass is claimed.

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

ASGI middleware tests also cover cancellation, request-context isolation, and process-local metrics. SSE integration tests additionally exercise loop ownership, pooled-engine disposal, cursor behavior, and process-local deployment-state isolation.

### Operational Verification

Exercises cover PostgreSQL backup/restore, projection rebuild, canonical audit, deployment preflight, readiness checks, SIGTERM shutdown, and incompatible schema revision detection.

There has been no production-traffic, availability, or real incident-response validation.

---

## Moneta Demo UI

Moneta is a lightweight **HTML/CSS/JavaScript interface** served by FastAPI at `GET /demo`.

It is a presentation layer over existing APIs, **not the core engineering deliverable**.

| **Screen**     | **Data source**                              | **V1 boundary**                                   |
| -------------- | -------------------------------------------- | ------------------------------------------------- |
| Login          | Keycloak OIDC Authorization Code + PKCE      | Legacy password login is unavailable through Kong |
| Dashboard      | `GET /summary`                               | Current API summary                               |
| Accounts       | `GET /accounts`, `GET /projections/accounts` | Missing projections shown explicitly              |
| Transactions   | `GET /transactions`                          | Read-only; first 100 oldest-by-ID results         |
| Reconciliation | `GET /reconciliations/{id}`                  | Lookup by ID, no listing endpoint                 |
| Engineering    | Six static cards                             | Not a live operations console                     |

The Engineering cards cover canonical financial state, idempotency and concurrency, append-only history, reconciliation, API security and authorization, and reliability verification.

Browser JWTs are held only in memory. Logout clears displayed personal data.

API errors produce visible error or empty states rather than fabricated fallback data.

The UI does not initiate financial writes in v1.

**Implementation references:**

- `backend/static/moneta.html`
- `backend/tests/test_moneta_demo.py`

---

## Technology Stack

| **Layer**                         | **Technology**                                      |
| --------------------------------- | --------------------------------------------------- |
| Financial Core                    | Python, FastAPI, Pydantic                           |
| AI Analysis Service               | Go                                                  |
| ORM and migrations                | SQLAlchemy, Alembic                                 |
| Authoritative database            | PostgreSQL                                          |
| Optional cache                    | Redis                                               |
| Frontend                          | React, TypeScript, Vite                             |
| Authentication and API gateway    | Keycloak, Kong                                      |
| Tests                             | pytest, Go testing, Playwright                      |
| Local orchestration and CI         | Docker Compose, GitHub Actions                      |

Financial services primarily use synchronous SQLAlchemy. The SSE activity reader uses SQLAlchemy async; separate native-async experiments do not constitute a full Financial Core rewrite.

Redis caching and the Keycloak/Kong integrations are optional infrastructure around the Financial Core; they are not components of its financial domain or authoritative datastore.

---

## Running Locally

### Prerequisites

Python/pip and PostgreSQL are required.

Redis is optional at runtime and required for cache integration tests.

Use separate development and disposable test databases. Some tests require database-creation or failure-injection privileges.

### Run the Full-System Browser E2E

The real React → Keycloak → Kong → FastAPI → PostgreSQL → Go flow requires Python 3.10+, Docker Engine with the Compose plugin, and Node.js/npm with the frontend dependencies and Playwright Chromium installed. See the [live E2E guide](gateway/e2e/README.live.md) for clean-checkout setup and isolation details.

From the repository root, run:

```bash
python3 gateway/e2e/run_live_e2e.py
```

The runner provisions a unique disposable Compose project, database, and volume, then removes only those run-specific resources. It verifies database identity and isolation; do not configure or adapt it to target protected development databases or existing application data.

### Install Dependencies

From the `backend` directory:

```
pip install -r requirements.txt
pip install -r requirements-test.txt

```

### Configure PostgreSQL

Set `DATABASE_URL` to a database intended for the current operation.

Example:

```
export DATABASE_URL="postgresql://USER:PASSWORD@localhost:5432/finance_dev_db"

```

Use a separate disposable database for tests.

### Apply Migrations

```
alembic upgrade head

```

Migrations targeting the protected `finance_db` database require explicit authorization, as documented in `backend/DEPLOYMENT_V1.md`.

Do not bypass database-safety checks when preparing a test environment.

### Run Tests

From the `backend` directory:

```
export DATABASE_URL="postgresql://USER:PASSWORD@localhost:5432/finance_test_db"
python -m pytest -q

```

For Redis integration tests, use a dedicated disposable Redis database:

```
export REDIS_TEST_URL="redis://127.0.0.1:6379/15"
export REQUIRE_REDIS_TESTS=1

python -m pytest -q tests/test_redis_fault_injection_correctness.py

```

Redis test fixtures may flush the configured test database.

**Warning:** Tests may create, modify, or destroy data. Never point the test runner at a database containing important records.

### Start FastAPI

```
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

```
docker compose -f gateway/compose.http.yml up -d --build

```

**External Authorization Lab**

```
docker compose -f gateway/compose.internal.yml up -d --build

```

Both configurations bind Kong to `127.0.0.1:8004` and must **not** run concurrently.

The API container is not directly published to a host port in these configurations.

The Gateway Labs require their documented database, Keycloak, network, and authentication configuration prerequisites.

For integrated Kong + Keycloak operation, clients authenticate using Keycloak OIDC.

Legacy password login and registration routes are unavailable through Kong.

See [`gateway/README.md`](https://github.com/duyhunga1k45pbc-cloud/personal-finance-platform/blob/main/gateway/README.md) for complete setup and security boundaries.

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
- Cross-instance SSE fan-out or production-scale long-lived connection availability.
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

**Development disclosure:** AI coding assistants, including GitHub Copilot, assisted with portions of implementation, test creation, and documentation.
