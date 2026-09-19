Personal Finance Platform

A reliability-focused personal finance backend built with FastAPI, PostgreSQL, and SQLAlchemy.

This project started as a conventional income/expense CRUD API and evolved into a backend engineering case study focused on a harder question:

How do we keep financial state correct when requests retry, users correct data, external providers disagree, workers fail, projections become stale, databases are restored, or new releases are deployed?

The project emphasizes correctness, auditability, recoverability, and operational safety rather than feature count.

What this project demonstrates

The system is designed around several classes of correctness:

Business correctness — financial events preserve their intended meaning.

State correctness — retries, concurrency, corrections, and external updates do not silently corrupt financial state.

Security correctness — only valid identities and authorized requests can cause state transitions.

Operational correctness — recovery, deployment, and runtime failures are handled explicitly and fail safely.

The implementation uses a modular-monolith architecture so that domain behavior remains understandable and testable without introducing distributed-system complexity that is not justified by the current scope.

Why financial correctness is hard

A finance backend has to deal with more than storing rows.

Examples of questions the system is designed to handle include:

Can a retried request create duplicate state changes?

Can concurrent operations produce an invalid balance?

How should corrections, refunds, reversals, and transfers affect financial state?

What happens when external data is stale, conflicting, or incomplete?

Can derived balances and summaries be rebuilt from trusted state?

Can a restored database be verified rather than merely assumed to be correct?

Can a new release safely start against the current database schema?

These failure modes drive the design.

High-level architecture

External systems
       |
       v
Data ingestion
       |
       v
Normalization
       |
       v
Domain / financial state
       |
       v
Derived views
       |
       v
API

Cross-cutting concerns:
security · auditability · observability · recovery

Detailed internal workflows, reusable components, and production-specific implementation details are intentionally omitted from this public portfolio repository.

Key engineering properties

Deterministic state transitions

Important financial changes are modeled explicitly so that business semantics remain understandable and testable.

The system distinguishes operations such as income, expenses, transfers, refunds, reversals, and adjustments instead of treating every row as an interchangeable transaction.

Idempotency and concurrency protection

Retried commands are protected from creating duplicate state transitions.

Concurrent changes are guarded with transactional mechanisms so that invalid financial state is prevented from being committed rather than merely detected after the fact.

Explicit reconciliation

Internal financial state and externally observed state are treated as separate concerns.

Disagreement is surfaced explicitly and resolved through controlled workflows rather than silently rewriting history to make numbers appear consistent.

Rebuildable derived state

Balances and summaries are treated as derived views rather than the ultimate source of truth.

If derived state becomes stale or invalid, it can be rebuilt and verified from canonical financial state.

Failure-aware operations

The project includes operational safeguards around:

readiness and schema compatibility,

backup and restore verification,

observability for correctness-relevant failures,

graceful shutdown,

deployment smoke testing,

fail-closed behavior when important assumptions are violated.

The public repository intentionally focuses on the engineering case study rather than exposing the complete internal operating model.

Verification

Current V1 verification includes 179 automated tests.

Coverage includes areas such as:

account ownership,

financial event semantics,

transfers,

refunds and reversals,

idempotency,

concurrency,

reconciliation,

provider-related state handling,

derived projections,

authentication and authorization,

observability,

disaster recovery,

deployment correctness,

end-to-end acceptance scenarios.

In addition to automated tests, the project has completed:

a real PostgreSQL backup/restore drill,

projection deletion and rebuild verification,

canonical-state audit after restore,

deployment preflight validation,

real Uvicorn process smoke testing,

liveness and readiness verification,

SIGTERM shutdown testing.

Selected design principles

The project follows a small set of engineering principles:

Do not modify evidence merely to make state appear correct.

Derived state should be rebuildable from trusted state.

Uncertainty should remain explicit.

Concrete failure modes should justify added mechanisms.

Simplicity is a correctness strategy.

When expected and actual state diverge, find the first point of divergence.

The detailed decision records behind these principles are kept private; the public repository is intended to demonstrate the engineering approach and verified outcomes.

Technology

Python

FastAPI

PostgreSQL

SQLAlchemy

Alembic

Pydantic

JWT authentication

pytest

Uvicorn

Running locally

From the backend directory, configure a PostgreSQL database through DATABASE_URL.

Run the test suite:

DATABASE_URL=postgresql://USER:PASSWORD@localhost:5432/finance_test_db pytest -q

Run the API:

uvicorn app.main:app --reload

Current scope

V1 focuses on the backend correctness model under the currently implemented environment.

Potential future work includes production integrations, deployment infrastructure, monitoring, and user-facing product development. Those additions would introduce new real-world failure modes and would be evaluated using the same failure-driven engineering approach.

Portfolio note

This public repository is a portfolio-oriented technical showcase. Some reusable internal components, detailed implementation logic, design records, and production-specific architecture are intentionally omitted.

The goal is to demonstrate ownership, systems thinking, correctness engineering, and verification without publishing the complete implementation blueprint.