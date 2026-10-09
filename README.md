# Personal Finance Platform

**A correctness-focused backend engineering case study built with FastAPI, PostgreSQL, and SQLAlchemy.**

This project started as a conventional income/expense CRUD API and evolved into a backend engineering case study focused on a harder question:

**How can financial state remain correct when requests retry, concurrent operations conflict, data is corrected, external providers disagree, workers fail, database schemas evolve, or execution conditions change?**

The project emphasizes correctness, auditability, recoverability, and operational safety rather than feature count.

It follows a **modular-monolith architecture** to keep financial domain behavior explicit, understandable, and testable without introducing unnecessary distributed-system complexity.

---

## Engineering Focus

The system is organized around several areas of correctness:

| Area | Engineering concern |
|---|---|
| Business correctness | Financial events preserve their intended meaning and accounting relationships. |
| State correctness | Retries, concurrency, corrections, and external updates do not silently corrupt canonical financial state. |
| Security correctness | Authentication and authorization boundaries prevent unauthorized access and state transitions. |
| Operational correctness | Runtime failures, recovery, and incompatible database revisions have explicit, verifiable outcomes. |
| Execution correctness | Transaction, resource, and request-context behavior remain consistent across tested execution and failure scenarios. |

## Why Financial Correctness Is Difficult

A finance backend must do more than store records.

It must handle questions such as:

- Can a retried request create duplicate financial effects?
- Can concurrent operations both succeed against the same expected version?
- Can a failed operation leave partially committed financial state?
- How should corrections, refunds, reversals, and transfers affect financial history?
- What happens when external data is stale, ambiguous, or conflicting?
- Can derived balances and summaries be rebuilt from trusted state?
- Can database migrations preserve existing financial meaning?
- Can cancellation, resource exhaustion, or worker termination leave state inconsistent?
- Can different execution models preserve the same financial correctness contracts?

These failure scenarios drive the design and verification.

## High-Level Architecture

```text
                  External Data Providers
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
                 +----------+----------+
                 |                     |
                 v                     v
            Event History      Derived Projections
                 |                     |
                 +----------+----------+
                            |
                            v
                         FastAPI API
```

**Cross-cutting concerns:**

Authentication · Authorization · Idempotency  
Transaction Safety · Auditability · Recovery  
Readiness · Observability

The architecture separates canonical financial state, historical evidence, and rebuildable derived views.

---

## Key Engineering Properties

### Canonical Financial State

Financial operations are modeled through explicit domain semantics rather than interchangeable transaction records.

The system distinguishes income, expenses, transfers, refunds, reversals, corrections, and reconciliation adjustments.

Canonical events, signed financial entries, and historical records provide the foundation for verifying financial state transitions.

### Transactional Idempotency

Retried commands are protected from creating duplicate business effects through database-backed idempotency constraints and transactional receipt persistence.

Verification includes a scenario where the database commits successfully but the caller does not receive the expected response.

A retry using the same command identity must recover the committed outcome without applying the operation again.

### Concurrency Control

Concurrent financial corrections use PostgreSQL transaction semantics and expected-version guards.

The system is tested against conflicting updates, stale versions, and actual PostgreSQL lock contention.

Competing operations must not both successfully apply changes against the same expected canonical event version.

### Explicit Reconciliation

External financial evidence and internal canonical state are treated as separate concerns.

Conflicting or incomplete observations are surfaced explicitly rather than silently converted into authoritative financial changes.

Controlled reconciliation workflows preserve the distinction between observed evidence and accepted financial state.

### Rebuildable Derived State

Balances and summaries are derived from canonical financial state.

Projection data can be deleted, reconstructed, and verified against trusted source records.

### Security Boundaries

The application implements JWT-based authentication and user-scoped authorization.

Security verification includes token tampering, invalid credentials, deleted-user identities, and unauthorized cross-user access or modification.

The implementation is not presented as a complete OAuth2/OIDC identity platform or shared multi-service authorization framework.

### Schema Evolution Safety

Alembic manages schema changes and historical data transformations.

Verification covers transactional rollback after an injected migration failure, readiness rejection of incompatible revisions, and preservation of legacy financial semantics during canonical-state backfill.

These tests do not establish zero-downtime or rolling-upgrade compatibility.

---

## Verification

**205 automated tests passed in the latest reported local PostgreSQL-backed regression run, with 256 warnings.**

The suite covers business behavior, transactional correctness, authentication, authorization, runtime failures, schema evolution, async database execution, native-async financial operations, and async middleware behavior.

### Verification Coverage

| Area | Selected scenarios |
|---|---|
| Financial semantics | Canonical events, signed entries, transfers, refunds, reversals, corrections |
| Transaction correctness | Idempotent replay, duplicate-command conflicts, expected-version concurrency |
| Provider integration | Evidence handling, lifecycle state, checkpoints, reconciliation |
| Derived state | Projection rebuilding and canonical-state consistency |
| Security | JWT validation, account ownership, cross-user authorization |
| Runtime failures | Lost response after commit, worker termination, pool exhaustion, PostgreSQL deadlocks |
| Recovery | Backup/restore, projection rebuilding, canonical-state audits |
| Deployment | Revision compatibility, preflight checks, readiness, graceful shutdown |
| Schema evolution | Migration rollback, historical data backfill invariants |
| Async SQLAlchemy bridge | Cancellation, guarded writes, session ownership, pool recovery |
| Native-async financial operations | Transaction atomicity, idempotency, cancellation, lock contention, pool recovery |
| Python async middleware | Cancellation propagation, request-context cleanup, concurrent context isolation |

### Runtime Failure Verification

Fault-injection scenarios include:

- HTTP response loss after a successful commit, followed by idempotent retry.
- Connection pool exhaustion and recovery.
- Worker termination before and after transaction commit.
- Real PostgreSQL deadlock recovery using SQLSTATE `40P01`.
- Verification of canonical records, entries, event history, idempotency receipts, and provider checkpoints.

Worker-process failures are exercised directly.

PostgreSQL server crashes, host failures, and actual network or proxy disconnects are not covered by the reported fault-injection tests.

### Schema Evolution Verification

Migration tests use uniquely named disposable PostgreSQL databases.

They verify that:

- Injected failure after Task 3 schema changes and history backfill results in transactional rollback.
- Readiness rejects an incompatible Alembic revision.
- Legacy income and expense transactions retain their essential business meaning after migration into canonical financial state.

### Async SQLAlchemy Bridge Verification

Five isolated PostgreSQL-backed tests examine execution through SQLAlchemy `AsyncSession`.

They cover:

- Cancellation before commit.
- Simulated acknowledgment loss after commit and idempotent replay.
- Competing guarded writes with observed PostgreSQL lock contention.
- Connection pool exhaustion and recovery.
- Independent async session ownership and transaction isolation.

**Scope:** These tests use `AsyncSession.run_sync()` to invoke existing synchronous application services.

They verify selected async-driver and transaction-lifecycle behavior, not a native-async application rewrite.

### Native Async Runtime Failure Verification

Five additional PostgreSQL-backed tests exercise a **test-only native-async financial correction implementation** using `async def`, `await`, and SQLAlchemy `AsyncSession` without `run_sync()` for the financial operation.

The experiment evaluates whether financial correctness contracts remain valid under an asynchronous execution model.

**Verified scenarios include:**

**1. Financial correction and idempotency**

- Correct financial state transitions.
- Canonical event and legacy transaction consistency.
- Idempotent command replay.
- Conflicting reuse of an idempotency key.

**2. Cancellation before commit**

- Controlled cancellation after transactional writes but before commit.
- Rollback of uncommitted financial changes.
- Verification of persisted state through an independent database observer.
- Session and connection cleanup.

**3. Simulated post-commit acknowledgment loss**

- Successful database commit followed by an injected response failure.
- Idempotent replay of the already committed result.
- Prevention of duplicate financial effects.

**4. Concurrent guarded writes**

- Competing corrections against the same expected event version.
- Actual PostgreSQL lock contention.
- One successful writer and rejection of a stale competing operation.
- Preservation of canonical financial history.

**5. Connection pool exhaustion and recovery**

- Controlled async connection-pool exhaustion.
- Timeout behavior under resource contention.
- Subsequent recovery and successful connection acquisition.

**Result: 5/5 native-async tests passed.**

These tests use disposable PostgreSQL databases migrated to the current schema revision.

**Scope and limitations:**

- The native-async financial operation is an isolated test implementation, not the production correction endpoint.
- The production financial services remain primarily synchronous.
- The experiment verifies selected financial contracts under native-async execution.
- Simulated post-commit acknowledgment loss does not establish correctness for every ambiguous network or database-commit failure.
- These results do not represent a completed application-wide native-async migration.

### Python Async Middleware Verification

Two tests exercise the application's actual ASGI observability middleware.

**Cancellation and request-context cleanup**

A test cancels an in-flight middleware invocation at a controlled await boundary.

It verifies that cancellation propagates, cleanup executes, and the request-ID `ContextVar` is reset.

Context cleanup is checked inside the same task, preventing a false positive caused by creating a fresh task with an empty context.

**Concurrent request-context isolation**

Two middleware invocations execute concurrently with separate request IDs.

Deterministic synchronization gates ensure their execution overlaps.

The tests verify that:

- Each request preserves its own correlation ID.
- Responses contain the appropriate request IDs.
- Request context does not leak between tasks.
- Process-local request metrics record the expected increment.

These tests cover actual middleware execution with controlled downstream coroutines.

They do not establish behavior for real client disconnects through a live ASGI server or cancellation of synchronous database operations.

### Operational Verification

Additional verification exercises include:

- Real PostgreSQL backup and restore.
- Projection deletion and rebuild.
- Canonical-state audit after recovery.
- Deployment preflight validation.
- Uvicorn process smoke testing.
- Liveness and readiness checks.
- SIGTERM shutdown testing.

Passing tests provide evidence for the scenarios exercised, not a mathematical proof of correctness across every possible execution or failure mode.

---

## Technology Stack

| Layer | Technology |
|---|---|
| Language | Python |
| API | FastAPI, Pydantic |
| Database | PostgreSQL |
| ORM | SQLAlchemy |
| Schema migrations | Alembic |
| Authentication | JWT |
| Testing | pytest |
| Async verification | asyncio, SQLAlchemy AsyncSession, asyncpg |
| Runtime | Uvicorn |
| Continuous integration | GitHub Actions |

The application primarily uses synchronous SQLAlchemy services.

Async database behavior is evaluated through isolated bridge and native-async experiments, while native asyncio behavior is also tested in the application's HTTP middleware.

---

## Running Locally

From the `backend` directory, configure PostgreSQL through `DATABASE_URL`.

**Install dependencies:**

```bash
pip install -r requirements.txt
pip install -r requirements-test.txt
```

**Apply database migrations:**

```bash
alembic upgrade head
```

**Run the test suite using a dedicated PostgreSQL test database:**

```bash
DATABASE_URL=postgresql://USER:PASSWORD@localhost:5432/finance_test_db pytest -q
```

Some tests create disposable PostgreSQL databases and require database-creation privileges.

Certain fault-injection tests require elevated PostgreSQL privileges.

**Start the API:**

```bash
uvicorn app.main:app --reload
```

Do not run destructive tests against databases containing important data.

---

## Engineering Principles

- Preserve business meaning across state transitions.
- Prevent invalid commits rather than relying exclusively on later detection.
- Keep external evidence separate from accepted financial state.
- Make uncertainty and conflicting observations explicit.
- Keep derived data rebuildable from trusted records.
- Verify behavior at transaction, concurrency, execution, and failure boundaries.
- Preserve business correctness contracts when evaluating alternative execution models.
- Prefer concrete requirements and observed failures over speculative complexity.
- Investigate the first divergence between expected and actual system state.

---

## Project Scope

Personal Finance Platform is a **backend engineering case study**, not a production-deployed financial product.

Its scope is deliberately focused on correctness mechanisms, real PostgreSQL behavior, controlled failure experiments, and reproducible verification.

The project does not claim exhaustive production validation, a fully native-async application, comprehensive distributed infrastructure, or complete coverage of all possible security and runtime failure modes.

## Portfolio Note

The repository serves as a technical showcase of implemented backend behavior and its verification evidence.

Some architecture decision records and exploratory research notes are maintained separately.

The public documentation focuses on observable engineering properties, reproducible tests, and clearly stated limitations.