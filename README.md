Personal Finance Platform
A correctness-focused financial backend engineering case study built with FastAPI, PostgreSQL, and SQLAlchemy.
This project explores how financial state can remain consistent and recoverable when requests retry, concurrent operations conflict, transactions fail, external evidence disagrees, or execution conditions change.
The backend implements financial event modeling, transaction and concurrency controls, idempotent commands, reconciliation, recovery tooling, authorization, and optional Redis caching. The repository includes PostgreSQL-backed integration tests, failure-injection scenarios, and performance measurements for selected operations.
Project status: Engineering portfolio and experimental application. Not production-deployed.
Quick navigation: Over-refund · Redis revision cache · Connection pool RCA · Index & pagination · Moneta demo · Test results · Security & Authorization
Engineering Highlights
Area	Engineering evidence
Financial correctness	PostgreSQL locking, idempotent commands, append-only history, reconciliation
Cache consistency	PostgreSQL-revisioned Redis keys, ownership-before-cache checks, fault injection
Query behavior	Deterministic pagination and a measured composite-index change
Reliability	Connection-pool lifecycle, failure injection, recovery and migration checks
Security	FastAPI policy and resource ownership enforcement; optional Kong/Keycloak integration labs
Verification	PostgreSQL and Redis integration tests; selected async experiments and gateway full-stack E2E
Demo	Moneta read-only interface with six static Engineering cards


Latest reported local backend suite: 347 passed, 0 failed, 0 skipped in 136.83 seconds, using PostgreSQL-backed tests and real Redis integration. This local run is not tied to a verified commit-specific CI run; CI evidence is listed separately under Test Results and Coverage.
The results apply to tested scenarios; they do not establish exhaustive correctness or production readiness.
Case Study: Preventing Concurrent Over-Refunds
One of the project's most important findings was a reproducible concurrency defect in the refund workflow.
The Business Invariant
For an original expense, the total amount of active refunds must not exceed the original refundable amount.
For example:
- Original expense: 100
- Refund request A: 60
- Refund request B: 60
Both refunds must not succeed.
The Failure
Before the concurrency fix, two independent PostgreSQL transactions could execute the following sequence:
1. Transaction A reads the remaining refundable balance as 100.
2. Transaction B independently reads the same remaining balance as 100.
3. Both transactions validate their refund amounts against the stale balance.
4. Both create their own refund events and causal links.
5. Both commit successfully.
The resulting active refund total becomes 120 against an original expense of 100.
Each individual transaction is atomic, but the aggregate business invariant is violated.
This demonstrates that transaction atomicity and idempotency do not, by themselves, guarantee correctness across concurrent operations.
Root Cause
Refund eligibility was evaluated without first serializing commands against their shared original financial event.
The application already implemented the business invariant for sequential execution. The missing guarantee was concurrency protection around the eligibility check and subsequent write.
The Fix
The refund and reversal paths now acquire a PostgreSQL SELECT FOR UPDATE row lock on the original financial event before evaluating eligibility.
The lock remains held until the transaction commits or rolls back.
Related correction and void operations follow the same original-event locking convention before evaluating causal dependencies.
Under the verified PostgreSQL READ COMMITTED isolation level, a competing transaction waits for the lock and then reevaluates eligibility against the committed state.
Preserving Idempotency
The serialization change also exposed an interaction with concurrent idempotent retries.
Two requests using the same command identity may compete for the original-event lock.
After the winning request commits, the waiting request must recover the already committed command result rather than fail merely because the refundable balance has changed.
The implementation includes receipt rechecking after causal rejection and rollback to preserve the same-key replay contract.
Verification Evidence
A deterministic PostgreSQL regression test was added to reproduce the original failure and verify the corrected behavior.
The tests confirm:
- The vulnerable implementation permitted two successful 60-unit refunds against an expense of 100.
- The corrected implementation prevents both conflicting refunds from committing.
- PostgreSQL lock contention is observed.
- The persisted refund aggregate remains within the original amount.
- Concurrent same-key retries recover the winning response without duplicating financial effects.
- Competing refund and reversal commands respect the shared serialization boundary.
- Relevant canonical records, causal links, historical records, and audit state remain consistent in the tested scenarios.
The original fix was verified using a targeted PostgreSQL regression suite; current reported regression results appear in Test Results and Coverage.
Implementation references:
- backend/app/causal_service.py
- backend/app/canonical_service.py
- backend/app/routers/causal_events.py
- backend/tests/test_refund_concurrency.py
- backend/tests/test_async_sqlalchemy_correctness.py
This case study documents a verified failure, its root cause, an implemented correction, and regression evidence. It does not claim that all possible concurrency defects have been eliminated.
Case Study: Transaction-Consistent Redis Caching
A provider external-transaction listing had measurable read-side costs. Removing an N+1 pattern reduced the query count, and a Redis prototype accelerated high-hit workloads. But a financial-data cache is useful only if it does not silently serve an outdated version after a committed write.
The Failure Boundary
An invalidation sequence such as COMMIT followed by Redis DEL contains a crash window: the database commit can succeed while the cache invalidation never happens. A TTL bounds how long the error persists; it does not establish read consistency.
The Design
Each provider connection has a listing revision stored in PostgreSQL. Database triggers advance it in the same transaction as inserts into provider transactions or evidence. A rollback rolls back the revision as well.
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


An in-flight reader can repopulate an older revision key, but subsequent requests look up the newer revision. Redis outages fall back to PostgreSQL; ownership verification takes place before Redis access. The cache is optional and disabled without REDIS_URL.
Verification and Trade-offs
PostgreSQL and Redis integration tests cover rollback, savepoints, concurrent writers, cross-user isolation, stale in-flight cache fills, malformed cache data, and Redis failure. A controlled local experiment (20 users, 300 transactions per connection, 6 concurrent clients) measured 144 → 271 RPS for read-only traffic and 126 → 198 RPS for mixed read/write traffic. These synthetic results depend on cache hit rate and are not production SLOs.
The design still makes PostgreSQL authorization/revision queries on a cache hit, serializes revision updates for the same connection, and retains older Redis keys until their TTLs expire. The provider listing still needs pagination for very large histories.
Implementation references: backend/app/listing_cache.py, migration k1b9c6d3f477, backend/tests/test_provider_listing_cache.py, backend/tests/test_provider_listing_revision_concurrency.py, and backend/tests/test_redis_fault_injection_correctness.py.
Case Study: Connection Pool Starvation
The Symptom and Root Cause
During Redis benchmarks, throughput degraded sharply under higher concurrency despite little active PostgreSQL query work. Instrumentation showed that a request could hold two database connections simultaneously: a FastAPI authentication dependency checked out one, then business logic attempted to check out a second before the first was returned.
At sufficiently high concurrency this produced connection pool starvation, not a slow SQL query or database row-lock bottleneck.
Evaluated Approaches
Approach	Observed result	Trade-off
Overlapping authentication and business sessions	Pool starvation under the tested load	Separate transactions but unnecessary simultaneous connections
Single shared session	Removed observed pool exhaustion	Authentication and business share transaction lifecycle; business rollback can expire ORM user state
Short-lived auth session + immutable Principal	Selected; no pool timeouts in tested runs	Sequential connection checkouts with separate transaction boundaries; the pool may reuse one physical connection


The selected design validates the JWT, loads the user in a short-lived authentication session, copies the needed attributes to an immutable Principal, closes the authentication session, and only then opens the business session. Authorization consumes verified identity values rather than a live ORM User object.
Verification and Limitations
Lifecycle tests verify that authentication returns its connection before business checkout and that error and rollback paths preserve the intended boundary. Concurrent runs through 32 virtual users reported no pool timeouts after the change. This establishes the tested lifecycle improvement, not unlimited throughput or production availability.
Implementation references: backend/app/routers/auth.py, backend/tests/test_auth_session_lifecycle.py.
Case Study: Deterministic Pagination and PostgreSQL Indexing
An index audit of the transaction-listing endpoint uncovered two separate issues: an expensive ownership-scoped query for some users and an API response whose page ordering was not guaranteed.
The Correctness Boundary
The original GET /transactions query applied LIMIT and OFFSET without an explicit ORDER BY. PostgreSQL does not guarantee row order in this case. On a seeded audit database, changing the join plan changed which transaction IDs appeared at the same offset, even though the underlying data was unchanged.
Phase A defines a complete ordering contract using transactions.id ASC, with the primary key serving as a unique tie-breaker. This improves deterministic paging for an unchanged dataset; it does not prevent offset drift when concurrent inserts occur between requests. Ascending ID also means the first 100 results are the oldest by ID, not the newest, which is relevant to Moneta's presentation.
The Query and Index Decision
The application filters the listing by user_id and now sorts by id:
SELECT ...
FROM transactions
JOIN financial_events ON ...
WHERE transactions.user_id = :user_id
ORDER BY transactions.id ASC
LIMIT :limit OFFSET :skip;

This is a schematic excerpt of the application query, not an exact copy of SQLAlchemy's full generated SQL; the benchmark replay uses the generated SQL, including the real joins and filters.
The audit then evaluated a composite B-tree index on transactions(user_id, id). The index was added in migration l2c0d7e4a588 using CREATE INDEX CONCURRENTLY inside an Alembic autocommit block, with explicit handling of an interrupted invalid build. Upgrade, downgrade and repeat upgrade were tested on a disposable database.
Measurement and Trade-offs
Selected EXPLAIN (ANALYZE, BUFFERS) measurements from a synthetic PostgreSQL audit database containing about 498,000 transactions (median of nine warm-cache runs):
Request/query component	Before index	After index
User with ~100 transactions, first page	21.93 ms	0.02 ms
User with ~10,000 transactions, first page	27.24 ms	19.24 ms
User with ~10,000 transactions, offset 5,000	36.77 ms	20.80 ms
User with ~100,000 transactions, offset 50,000	17.66 ms	17.60 ms


These are SQL execution measurements, not HTTP end-to-end latency, and performance varied with user cardinality and plan choice. The index occupied approximately 11 MB on the seeded database; one insert experiment showed approximately 10% additional write time. Large offsets and the COUNT join for very large users remain expensive.
Verification and Scope
Five new API pagination tests cover ordering, page boundaries, filters, authorization and ownership. They all passed, but also passed when ORDER BY was temporarily removed on the small test fixture; therefore, the separate large-dataset plan-change reproduction is the stronger evidence for the original non-determinism. The change was merged into main via PR #6 on 2026-10-12. That merge does not, by itself, verify the CI status of the latest Redis-fault-injection revision.
The same audit identified four redundant non-unique indexes whose key definitions overlap primary-key or unique-constraint indexes. They were not removed in Phase A. The associated database constraints remain in place; cleanup is deferred and would require its own reviewed migration.
Implementation references: backend/app/routers/transactions.py, backend/alembic/versions/l2c0d7e4a588_transactions_user_id_index.py, backend/tests/test_transactions_pagination.py, and docs/transactions_index/README.md (created during the Phase A audit).
System Structure
The application is implemented as a modular monolith.
Its design keeps financial business rules and transaction boundaries explicit without introducing distributed infrastructure that is unnecessary for the project's current scope.
The financial backend is a modular monolith. Kong Gateway and Keycloak are external integration components, not independently deployed financial business services.
The main responsibilities are separated into:
Component	Responsibility
FastAPI routers	HTTP request handling, authentication dependencies, validation, and responses
Business services	Financial operations, domain invariants, causal rules, and transaction coordination
Canonical financial state	Authoritative financial events, entries, and relationships
Financial history	Persistent historical evidence of state changes
Provider integration	External data ingestion, normalization, and reconciliation
Derived projections	Rebuildable balances and financial summaries
Operational mechanisms	Idempotency, readiness, migrations, auditing, and recovery


A simplified flow distinguishes external evidence from validated financial state:
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


Normalization alone does not make provider observations authoritative financial events. Interpretation and validated transitions are distinct responsibilities; external evidence can also be reconciled against canonical records without being silently accepted. Client-initiated commands enter via FastAPI and must respect the same domain invariants and explicit transaction boundaries.
Cross-cutting concerns include authentication, authorization, idempotency, concurrency control, auditability, and operational recovery.
The project does not claim to implement a production distributed event-processing platform.
Core Engineering Properties
Financial events and signed entries are the source for balances and summaries; projections can be rebuilt from canonical records. Commands use database-backed idempotency, expected-version checks and PostgreSQL locking. Reconciliation keeps external observations distinct from accepted financial state, surfacing stale, incomplete or conflicting evidence rather than silently treating it as authoritative. The refund case study shows where these mechanisms interact under concurrency.
Security Boundaries
Authentication identifies the caller; authorization limits operations to owned resources. The route-policy table and immutable Principal are implemented in backend/app/authorization.py and backend/app/routers/auth.py. SQL ownership predicates are applied before joins, counts, pagination and aggregation. Policies cover HTTP routes and are kept consistent with handlers by tests; ownership checks remain distributed through service queries. See backend/SECURITY_V1.md for the policy boundaries and PostgreSQL-backed cross-user tests.
Kong and Keycloak are optional integration experiments, not prerequisites for financial state correctness. The Gateway Lab has two explicit modes:
- HTTP Infrastructure Lab (gateway/compose.http.yml): Kong handles API routing, request correlation IDs, IP-based rate limiting, and JWT verification. It does not call the external authorization endpoint; FastAPI continues to enforce route authorization and SQL resource ownership.
- External Authorization Lab (gateway/compose.internal.yml): Preserves the Kong external-authorization pilot, including fail-closed behavior and its route-specific policy experiment. This is not a reusable cross-service authorization platform.
Both modes proxy to the FastAPI backend; neither changes financial transaction rules. Deployment, login-route scope, limitations and manual signing-key rotation are documented in gateway/README.md.
Alembic verification covers transactional rollback, canonical-history backfill, readiness rejection of incompatible revisions, and concurrent index creation with downgrade and invalid-build recovery. Test databases reject the protected finance_db target, and production migrations require explicit authorization. These safeguards do not establish zero-downtime or rolling-upgrade compatibility.
Test Results and Coverage
The repository includes unit, integration, concurrency, failure-injection, and end-to-end tests. Database-sensitive scenarios use PostgreSQL; optional cache tests include Redis integration. The reported outcomes apply only to the conditions exercised.
Local Test Results
The latest reported local backend suite completed with 347 passed, 0 failed, 0 skipped in 136.83 seconds, using PostgreSQL-backed tests and real Redis. This result is not tied to a verified commit-specific CI run. Separately, the Kong authorization integration suite had 8 passed, and gateway full-stack E2E had 2 passed; neither count is included in the backend suite result. After Gateway Lab separation (PR #8), the new local static configuration tests passed 2/2, and a targeted local backend security/authorization/OIDC run passed 71/71. These are separate runs, not additions to the previously reported 347-test backend suite.
GitHub Actions Evidence
These commit-specific results are separate from local test counts:
- 4292cb2: Kong Authorization Integration Tests passed.
- 4292cb2: Gateway Full-Stack E2E passed.
- 4292cb2: Backend CI passed.
- 27bb173: Gateway Full-Stack E2E passed.
- 27bb173: Backend CI passed. The dedicated Kong integration workflow did not run for this commit.
The Kong integration workflow tests fail-closed handling of external-authorization timeout, connection failure, malformed responses and HTTP 500. E2E verifies an authenticated account read, a denied write with no database mutation, and deployment/E2E Kong configuration parity (normalizing the test API address and test-realm signing key). These results are evidence for the exercised scenarios, not production deployment or exhaustive security correctness.
The Gateway Lab separation was merged via PR #8 at commit e39eb79. On that branch, the existing Gateway Full-Stack E2E and Kong Authorization Integration workflows passed. A separate manual Docker exercise verified Moneta OIDC login and successful authenticated API reads through the HTTP Infrastructure Lab, plus rejection of a request lacking a JWT (401). This manual smoke test does not establish full cross-user E2E coverage for the new HTTP mode.
The broader suite covers financial transitions, idempotency, PostgreSQL concurrency, Redis fault injection, reconciliation, projections, authorization, runtime failures, recovery and schema evolution. The individual case studies describe the relevant counterexamples, measurements and trade-offs.
Additional Verification
Runtime Failure Verification
Failure tests exercise post-commit response loss and idempotent retry, pool exhaustion, worker termination before/after commit, and PostgreSQL deadlock recovery (40P01). They verify canonical records, history, receipts and provider checkpoints. They do not reproduce PostgreSQL server crashes, infrastructure outages or real network/reverse-proxy disconnects.
Async Execution Verification
The application's financial path uses synchronous SQLAlchemy. Separate PostgreSQL-backed experiments evaluate AsyncSession.run_sync() lifecycle and test-only native-async corrections across cancellation, response loss, conflicting writes, lock contention and pool recovery. The native-async experiment passed 5/5 tests; it is not the production correction endpoint or an application rewrite. ASGI middleware tests cover cancellation, request-context cleanup/isolation and process-local metrics, not every real client disconnect or cancellation of synchronous database work.
Operational Verification
Exercises cover PostgreSQL backup/restore, projection rebuild and canonical audit, deployment preflight, process/readiness checks, SIGTERM shutdown and incompatible-revision detection. There has been no production-traffic, availability or incident-response validation.
Moneta Demo UI
Moneta is a lightweight HTML/CSS/JavaScript interface served at GET /demo. It is a presentation layer over existing APIs, not the core engineering deliverable.
Screen	Data source	V1 boundary
Login	Keycloak OIDC Authorization Code + PKCE	Legacy POST /auth/login and POST /auth/register are unavailable through Kong
Dashboard	GET /summary	Shows current API summary, not all canonical projections
Accounts	GET /accounts, GET /projections/accounts	Missing projections are shown explicitly, not fabricated
Transactions	GET /transactions	Read-only; first 100 oldest-by-ID results under the merged Phase A ordering
Reconciliation	GET /reconciliations/{id}	Lookup by ID; no listing endpoint
Engineering	Six static cards	Not a live operations console


The Engineering cards are Canonical financial state, Idempotency & concurrency, Append-only history, Reconciliation, API Security & Authorization, and Reliability & Verification.
The browser keeps JWTs only in memory; logout clears displayed personal data. API errors yield visible error/empty states instead of a fake-data fallback. The UI intentionally does not initiate financial writes in v1.
Implementation references: backend/static/moneta.html, backend/tests/test_moneta_demo.py.
Technology Stack
The backend uses Python, FastAPI, Pydantic, SQLAlchemy, PostgreSQL and Alembic. Redis is an optional listing cache. Kong and Keycloak are optional external integration components for HTTP infrastructure and authorization-boundary experiments. Tests use pytest; the UI is static HTML/CSS/JavaScript served by FastAPI. Financial services primarily use synchronous SQLAlchemy; native-async behavior is evaluated separately.
Running Locally
Prerequisites
Python/pip and PostgreSQL are required. Redis is optional at runtime and needed for cache integration tests. Use separate development and disposable test databases; some tests need database-creation or fault-injection privileges.
Install Dependencies
From the backend directory:
pip install -r requirements.txt
pip install -r requirements-test.txt

Configure PostgreSQL
Set DATABASE_URL to a PostgreSQL database intended for the current operation.
For example:
export DATABASE_URL="postgresql://USER:PASSWORD@localhost:5432/finance_dev_db"

Use a separate, disposable database for tests.
Apply Migrations
alembic upgrade head

Migrations targeting the protected finance_db database require explicit authorization as documented in backend/DEPLOYMENT_V1.md.
Do not bypass database-safety checks when preparing a test environment.
Run Tests
From the backend directory, use a disposable PostgreSQL test database:
export DATABASE_URL="postgresql://USER:PASSWORD@localhost:5432/finance_test_db"
python -m pytest -q

To reproduce the Redis-related suite, start a local Redis instance and use a dedicated disposable Redis database (database 15 is an example, not a requirement):
export REDIS_TEST_URL="redis://127.0.0.1:6379/15"
export REQUIRE_REDIS_TESTS=1

python -m pytest -q tests/test_redis_fault_injection_correctness.py
python -m pytest -q

Redis fixtures may flush the configured Redis test database; never use a shared or valuable database. The reported 347-test result is local, environment-dependent evidence, and some experiments require additional PostgreSQL privileges.
Warning: Tests may create, modify, or destroy data. Never point the test runner at a database containing important records.
Start the API
uvicorn app.main:app --reload --port 8765

Uvicorn starts the standalone FastAPI application only; it does not start Kong or Keycloak. In this mode, create an account through POST /auth/register and authenticate with the local email/password flow (POST /auth/login), available through Moneta's login form or the API. Moneta also has a Keycloak OIDC login option for local development, which requires Keycloak to be running separately.
For the optional Kong + Keycloak labs, clients authenticate with Keycloak OIDC and access FastAPI through Kong. The HTTP Infrastructure Lab uses gateway/compose.http.yml; the separate External Authorization Lab uses gateway/compose.internal.yml. Both configurations publish Kong on 127.0.0.1:8004 and must not run concurrently. Neither exposes the API container directly on a host port. Legacy password login and registration routes are unavailable through Kong. See gateway/README.md for setup, prerequisites, and security boundaries.
Visit http://127.0.0.1:8765/demo for Moneta or http://127.0.0.1:8765/docs for Swagger. Moneta shows an empty financial history until actual transactions exist.
Redis is optional for the running application: set REDIS_URL only when provider listing caching is needed. Integration tests use the separate REDIS_TEST_URL and REQUIRE_REDIS_TESTS=1 settings shown above.
Scope and Limitations
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
- Production operation of the Kong/Keycloak integration, automatic Kong JWKS rotation, or zero-downtime signing-key rotation; key rotation is manual, as documented in gateway/README.md
- Production-grade performance, availability, or operational readiness
- Reliable cache hit rates under unknown real-world workloads
- Pagination for extremely large provider listing histories
- Stable offset pagination under concurrent writes; GET /transactions uses ID-ascending order and may require a separate newest-first UI query contract
- Proven improvement for every database workload: the new composite index adds write/storage cost and offers limited gains for some high-cardinality users
- Full financial write/reconciliation workflows from the Moneta UI
Some guarantees also depend on application write paths following shared locking and transaction conventions. Direct database writes or future code paths that bypass those conventions require separate verification.
The public repository emphasizes implemented behavior, reproducible tests, documented engineering decisions, and explicit limitations. In particular, the Redis fault-injection results are evidence for selected failures survived, not a proof that Redis cannot contribute to future correctness defects.
Development disclosure: GitHub Copilot assisted with portions of implementation, test creation, and documentation.
