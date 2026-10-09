# Personal Finance Platform

**A correctness-focused backend engineering case study built with FastAPI, PostgreSQL, and SQLAlchemy.**

This project started as a conventional income/expense CRUD API and evolved into an exploration of a more fundamental engineering problem:

**How can financial state remain correct when requests retry, concurrent operations conflict, data is corrected, external providers disagree, workers fail, database schemas evolve, or execution mechanisms change?**

The project prioritizes **state correctness, transaction safety, auditability, recoverability, and verification** over feature count.

It uses a modular-monolith architecture to keep domain behavior explicit and testable without introducing unnecessary distributed-system complexity.

## Engineering Focus

The case study investigates five areas of correctness:

| Area | Engineering concern |
|---|---|
| **Business correctness** | Financial operations must preserve their intended meaning and accounting relationships. |
| **State correctness** | Retries, concurrent writes, corrections, and external updates must not silently corrupt canonical state. |
| **Security correctness** | Authentication and authorization boundaries must prevent unauthorized state access and modification. |
| **Operational correctness** | Runtime failures, recovery procedures, and schema incompatibilities must have explicit, verifiable outcomes. |
| **Execution correctness** | Transaction and resource-management guarantees must be evaluated under different execution and failure conditions. |

The central approach is to identify system invariants, analyze the state transitions that could violate them, and verify the mechanisms intended to preserve those invariants.

## Why Financial Correctness Is Difficult

A financial backend must do more than persist records.

It must answer questions such as:

- Can a retried command cause duplicate financial effects?
- Can two concurrent updates both succeed against the same expected version?
- Can a failed operation leave partially committed financial state?
- How should transfers, refunds, reversals, and corrections affect canonical history?
- What happens when externally observed data is stale, inconsistent, or ambiguous?
- Can derived balances and summaries be rebuilt from trusted state?
- Can a database migration preserve existing business meaning?
- What remains correct when database connections, workers, or request processing fail?

These questions determine the engineering mechanisms and verification scenarios used throughout the project.

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
              +------------+------------+
              |                         |
              v                         v
        Event History             Derived Projections
              |                         |
              +------------+------------+
                           |
                           v
                      FastAPI API

Cross-cutting concerns:
Authentication · Authorization · Idempotency
Transaction Safety · Auditability · Recovery
Readiness · Observability
```

The architecture separates canonical financial state from historical evidence and rebuildable projections.

## Key Engineering Properties

### Canonical Financial State

Financial operations are represented through explicit domain semantics rather than interchangeable transaction records.

The system distinguishes income, expenses, transfers, refunds, reversals, corrections, and reconciliation adjustments.

Canonical events, signed financial entries, and historical records provide the foundation for verifying financial state transitions.

### Transactional Idempotency

Retried commands are prevented from producing duplicate business effects through database-backed idempotency constraints and transactional receipt persistence.

The verification suite includes scenarios where a command commits successfully but the caller does not receive the expected response.

Retries must recover the committed outcome without applying the financial operation again.

### Concurrency Control

Concurrent financial corrections use PostgreSQL transaction semantics and expected-version guards.

The system is tested against conflicting updates, stale versions, and actual PostgreSQL lock contention.

The intended invariant is that competing operations cannot both successfully apply changes against the same expected event version.

### Explicit Reconciliation

External financial evidence and internal canonical state are modeled separately.

Conflicting or incomplete observations are not automatically treated as authoritative financial changes.

Reconciliation workflows make disagreement explicit and support controlled resolution.

### Rebuildable Projections

Balances and summaries are derived from canonical financial state.

Projection data can be deleted, reconstructed, and checked against its source rather than treated as independent financial truth.

### Security Boundaries

The application implements JWT-based authentication and user-scoped access controls.

Security verification includes token tampering, expired or invalid credentials, deleted-user identities, and unauthorized cross-user resource access or modification.

The current implementation is not presented as a complete OAuth2/OIDC identity platform or a shared multi-service authorization framework.

### Schema Evolution Safety

Alembic migrations manage database schema changes and historical data transformations.

Verification includes:

- Transactional rollback after injected migration failure.
- Fail-closed readiness when the database revision does not match the application release.
- Preservation of legacy financial semantics during migration into canonical events and entries.

These tests verify selected migration contracts. They do not establish zero-downtime or rolling-upgrade compatibility.

## Verification

**198 automated tests passing in the latest reported local PostgreSQL-backed regression run.**

The test suite covers business behavior, transactional correctness, security, runtime failures, schema evolution, and isolated async execution scenarios.

### Selected Verification Areas

| Area | Verified scenarios |
|---|---|
| Financial semantics | Canonical events, signed entries, transfers, refunds, reversals, corrections |
| Transaction correctness | Idempotent replay, duplicate-command conflicts, expected-version concurrency control |
| Provider integration | Evidence handling, lifecycle state, checkpoints, reconciliation |
| Derived state | Projection rebuild and canonical-state consistency |
| Authentication and authorization | JWT validation, user ownership, cross-user access restrictions |
| Runtime failures | Lost response after commit, worker termination, pool exhaustion, PostgreSQL deadlocks |
| Recovery | PostgreSQL backup/restore, projection rebuilding, canonical-state audits |
| Deployment | Revision compatibility, readiness, preflight checks, shutdown behavior |
| Schema evolution | Transactional migration rollback, historical backfill invariants |
| Async SQLAlchemy | Cancellation, transaction lifecycle, guarded concurrency, pool recovery, session ownership |

### Runtime Failure Verification

Fault-injection tests examine:

- HTTP response loss after a successful commit, followed by an idempotent retry.
- Database connection pool exhaustion and subsequent recovery.
- Worker process termination before and after transaction commit.
- PostgreSQL deadlock recovery involving SQLSTATE `40P01`.
- Preservation of canonical events, financial entries, historical records, idempotency receipts, and provider checkpoints.

These experiments exercise specific failure boundaries. PostgreSQL server crashes, host failures, and actual network or proxy disconnects are not covered by the reported fault-injection tests.

### Schema Evolution Verification

Migration tests run against uniquely named disposable PostgreSQL databases.

They verify that:

- Injected failure after Task 3 schema changes and history backfill results in transaction rollback.
- Application readiness rejects an incompatible Alembic revision.
- Legacy income and expense transactions retain their essential business meaning after migration to canonical financial events.

### Async SQLAlchemy Verification

Five isolated PostgreSQL-backed tests examine async-driver execution through SQLAlchemy's `AsyncSession`.

They cover:

- Cancellation before transaction commit.
- Simulated response or acknowledgment loss after commit, followed by idempotent replay.
- Concurrent guarded writes with observed PostgreSQL lock contention.
- Connection pool exhaustion and recovery.
- Independent session ownership and transaction isolation between tasks.

**Scope limitation:** These tests use `AsyncSession.run_sync()` to invoke existing synchronous application services. They verify selected async-driver and transaction-lifecycle behavior, not a complete native-async application implementation.

### Operational Verification

The project has also exercised:

- Real PostgreSQL backup and restore.
- Projection deletion and rebuild.
- Canonical-state audit after recovery.
- Deployment preflight validation.
- Uvicorn process smoke testing.
- Liveness and readiness checks.
- SIGTERM shutdown behavior.

Passing tests provide evidence for the scenarios exercised, not a mathematical proof of correctness for every possible execution or failure mode.

## Technology Stack

| Layer | Technology |
|---|---|
| Language | Python |
| API | FastAPI, Pydantic |
| Database | PostgreSQL |
| ORM | SQLAlchemy |
| Migrations | Alembic |
| Authentication | JWT |
| Testing | pytest |
| Async experimentation | SQLAlchemy AsyncSession, asyncpg |
| Runtime | Uvicorn |
| CI | GitHub Actions |

The application primarily uses synchronous SQLAlchemy services. Async SQLAlchemy is evaluated in isolated verification tests.

## Running Locally

From the `backend` directory, configure a PostgreSQL connection using `DATABASE_URL`.

Install the application and test dependencies:

```bash
pip install -r requirements.txt
pip install -r requirements-test.txt
```

Apply database migrations:

```bash
alembic upgrade head
```

Run the PostgreSQL-backed test suite against a dedicated test database:

```bash
DATABASE_URL=postgresql://USER:PASSWORD@localhost:5432/finance_test_db pytest -q
```

Some tests create temporary PostgreSQL databases and may require additional database privileges.

Start the API:

```bash
uvicorn app.main:app --reload
```

Do not run destructive tests against databases containing important data.

## Engineering Principles

The project follows several principles:

1. **Preserve business meaning.** A technically successful write is not necessarily a valid financial state transition.
2. **Make uncertainty explicit.** Missing or conflicting evidence must not silently become authoritative truth.
3. **Prevent invalid commits.** Prefer transactional enforcement of correctness over detecting corruption afterward.
4. **Keep derived state rebuildable.** Reconstruct projections from trusted canonical records.
5. **Verify failure boundaries.** Test the consequences of retries, concurrency, crashes, and incomplete operations.
6. **Separate contracts from mechanisms.** Evaluate implementation choices against the invariants they are intended to preserve.
7. **Avoid unjustified complexity.** Additional infrastructure should address concrete requirements or demonstrated failure modes.

## Project Scope

Personal Finance Platform is an **engineering case study**, not a production-deployed financial product.

Its primary purpose is to explore and verify backend correctness using real PostgreSQL behavior and controlled failure scenarios.

It does not claim comprehensive production validation, complete distributed-system infrastructure, native-async application migration, or exhaustive security coverage.

The implementation and verification results represent the current case-study scope.

## Portfolio Note

This repository demonstrates a backend implementation and its corresponding verification evidence.

Some architecture decision records and exploratory research notes are maintained separately.

The focus is on observable engineering properties, reproducible tests, and clearly defined technical limitations rather than presenting an unverified production-readiness claim.