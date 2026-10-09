# Personal Finance Platform

**A correctness-focused financial backend engineering case study built with FastAPI, PostgreSQL, and SQLAlchemy.**

This project explores how financial state can remain consistent and recoverable when requests retry, concurrent operations conflict, transactions fail, external evidence disagrees, or execution conditions change.

It started as a conventional income/expense CRUD application and evolved into a backend engineering project focused on **business invariants, transaction boundaries, concurrency control, idempotency, recovery, and evidence-based verification**.

Rather than optimizing for feature count, the project emphasizes understanding failure modes, implementing explicit correctness guarantees, and testing those guarantees against real PostgreSQL behavior.

**Project status:** Engineering portfolio and experimental application. Not production-deployed.

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
| Security | JWT authentication, ownership enforcement, cross-user authorization verification |
| Schema evolution | Alembic migrations, rollback verification, historical backfill, revision compatibility checks |
| Async correctness | SQLAlchemy AsyncSession, cancellation, connection lifecycle, native-async experiments |

**Latest production-suite verification:** 208 automated tests passed on a disposable PostgreSQL database, with 270 warnings.

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

The full production test suite subsequently passed with **208 tests** on an isolated PostgreSQL database.

**Implementation references:**

- `backend/app/causal_service.py`
- `backend/app/canonical_service.py`
- `backend/app/routers/causal_events.py`
- `backend/tests/test_refund_concurrency.py`
- `backend/tests/test_async_sqlalchemy_correctness.py`

This case study documents a verified failure, its root cause, an implemented correction, and regression evidence. It does not claim that all possible concurrency defects have been eliminated.

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

### 8. Schema Evolution Safety

Database schema changes are managed with Alembic.

Verification includes:

- Transactional rollback after injected migration failures
- Preservation of historical financial meaning during canonical-state backfill
- Readiness rejection of incompatible schema revisions
- Migration tests using disposable PostgreSQL databases

The test infrastructure rejects the protected `finance_db` database as a pytest target.

Production-target migrations are blocked by default and require explicit per-invocation authorization.

These protections do not establish zero-downtime or rolling-upgrade compatibility.

---

## Verification Strategy

The project follows a failure-oriented verification approach:

1. Identify the business behavior and its correctness invariant.
2. Determine which transaction, concurrency, or execution conditions could violate it.
3. Construct a reproducible scenario.
4. Verify actual behavior against PostgreSQL where database semantics matter.
5. Introduce or correct the protection mechanism.
6. Rerun the scenario and relevant regression tests.
7. Record the verified guarantee and its limitations.

Tests are treated as evidence for defined claims, not proof that every possible system state has been explored.

### Latest Production Test Run

| Result | Value |
|---|---|
| Passed | 208 |
| Warnings | 270 |
| Database | Disposable PostgreSQL database |
| Schema | Migrated to the current Alembic head |
| Outcome | All selected production-suite tests passed |

This run excluded explicitly experimental research tests and private research artifacts.

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

These tests are separate from the reported 208-test production suite.

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

## Technology Stack

| Layer | Technology |
|---|---|
| Language | Python |
| API framework | FastAPI |
| Validation | Pydantic |
| Database | PostgreSQL |
| ORM | SQLAlchemy |
| Schema migrations | Alembic |
| Authentication | JWT |
| Testing | pytest |
| Async verification | asyncio, AsyncSession, asyncpg |
| Application server | Uvicorn |
| CI configuration | GitHub Actions |

The application primarily uses synchronous financial services. Native-async financial behavior is evaluated separately through isolated experiments.

---

## Running Locally

### Prerequisites

- Python and pip
- PostgreSQL
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

The default test command may include experimental suites with additional environmental requirements. The reported 208-test production-suite result excludes the explicitly experimental research files.

**Warning:** Tests may create, modify, or destroy data. Never point the test runner at a database containing important records.

### Start the API

```bash
uvicorn app.main:app --reload
```

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

Some guarantees also depend on application write paths following shared locking and transaction conventions. Direct database writes or future code paths that bypass those conventions require separate verification.

The public repository emphasizes implemented behavior, reproducible tests, documented engineering decisions, and explicit limitations.

## Portfolio Note

This repository is intended to demonstrate backend engineering through **concrete failure scenarios and verifiable correctness mechanisms**, rather than claims of production scale.

The primary emphasis is the relationship between:

**Business Requirements → Invariants → Failure Boundaries → Implementation → Verification Evidence**

The central engineering objective is not to eliminate all uncertainty, but to establish which correctness properties have been tested, under which conditions, and where additional evidence is still required.