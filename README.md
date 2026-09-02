Personal Finance Platform

A reliability-focused personal finance backend built with FastAPI, PostgreSQL, and SQLAlchemy.

The project started as a conventional income/expense CRUD API and evolved into a state-correct financial system designed around a harder question:

How do we keep financial truth correct when requests retry, users correct data, providers disagree, workers crash, projections become stale, databases are restored, or new releases are deployed?

The current V1 focuses on business correctness, state correctness, security correctness, and operational correctness rather than feature count.

Why this project exists

A simple finance app can store transactions. A reliable finance system also needs to answer:

What is the source of this financial state?

Is this raw evidence or an interpretation?

What happens when a transaction is corrected, refunded, or reversed?

Can retries create duplicates?

Can concurrent requests corrupt state?

What happens when an external provider sends stale or conflicting data?

Can derived balances be rebuilt from canonical truth?

Can a restored database be proven equivalent to the backup?

Can a new application release safely start against the current database schema?

The architecture is built around those failure modes.

Core state model

Reality
  ↓
Raw evidence
  ↓
Identity / deduplication
  ↓
Normalization
  ↓
Canonical interpretation
  ↓
Canonical financial state
  ↓
Rebuildable projections

Reconciliation runs alongside that flow:

Expected state
      ↕
Observed external state
      ↓
Mismatch
      ↓
Explicit resolution

The system follows a strict truth hierarchy:

Raw provider evidence is immutable.

Canonical interpretation can be corrected, but corrections are auditable.

Canonical financial state is derived from accepted interpretation.

External observed state is stored separately.

Reconciliation compares expected and observed state.

Projections are rebuildable and are never treated as canonical truth.

Financial semantics

The backend models financial events explicitly rather than treating every row as a generic transaction.

Supported event types include:

INCOME

EXPENSE

TRANSFER

REFUND

REVERSAL

ADJUSTMENT

Important invariants:

Internal transfers do not change net worth.

Credit-card purchases count as expenses when posted.

Credit-card repayments are transfers, not second expenses.

Refunds and reversals are new linked events; original events are preserved.

Provider evidence is never silently rewritten to make canonical state look correct.

Adjustments are explicit and reserved for unreconstructable gaps.

User-confirmed interpretations cannot be silently overwritten by automated classification.

State correctness

Idempotency

Retried commands are protected from producing duplicate state transitions.

The system distinguishes:

same idempotency key + same command
→ replay the previous result

same idempotency key + different command
→ conflict

Concurrency control

Concurrent state changes are protected with transactional mechanisms including optimistic version checks and row locking where required.

The goal is not merely to avoid exceptions, but to prevent invalid financial state from being committed.

Append-only history

Important state transitions preserve history instead of overwriting the past.

Examples include:

created

corrected

voided

provider lifecycle transitions

reconciliation transitions

interpretation transitions

Source lifecycle

Provider transactions use an explicit lifecycle:

PENDING
  ↓
POSTED
  ↓
REVERSED

Stale regressions are rejected instead of silently moving canonical state backward.

External provider pipeline

The provider integration layer is designed around immutable evidence and stable identity.

Provider payload
    ↓
Immutable external evidence
    ↓
Stable external identity
    ↓
Normalization
    ↓
Interpretation state machine
    ↓
Canonical event materialization

Provider sync also uses durable checkpoints so crash/retry behavior can be handled without corrupting progress or duplicating canonical state.

The V1 currently proves the provider pipeline with adapters and tests; connecting a real financial provider is a later product-integration step.

Reconciliation

Financial systems cannot assume internal state always matches external reality.

The reconciliation model makes disagreement explicit:

Expected balance
      vs
Observed balance
      ↓
UNKNOWN / RECONCILED / MISMATCH / RESOLVED

A mismatch is not hidden by silently changing canonical history.

Resolution can be based on:

a real missing financial event, or

an explicit adjustment when the gap cannot be reconstructed.

Rebuildable projections

Balances and summaries are treated as projections rather than primary truth.

The system can:

detect stale or missing projections,

rebuild them atomically from canonical state,

verify canonical fingerprints,

fail closed when a projection cannot be trusted.

This means a broken derived view does not require rewriting historical financial truth.

Security correctness

Security is treated as part of state correctness:

Who is allowed to cause which state transition?

V1 includes:

strict JWT validation,

issuer and audience validation,

expiration and timing claims,

token type checks,

user-bound subject identity,

production secret validation,

uniform authentication failures,

normalized registration identity,

duplicate-registration containment,

password boundary validation,

public/private endpoint boundary tests,

production documentation disabling,

sensitive-value log redaction,

security headers.

Rate limiting is intentionally not implemented as a fake in-memory production mechanism. A shared limiter should be selected after the real deployment topology is known.

Observability

The application exposes structured operational signals for correctness-relevant failures.

Examples include:

HTTP requests and 5xx responses,

database failures,

provider sync conflicts and failures,

reconciliation mismatches,

missing or stale projections,

projection rebuild results,

sync checkpoint age,

durable operational counts.

Health endpoints:

GET /health/live
GET /health/ready

/health/live answers whether the process is alive.

/health/ready verifies that the process is ready to serve traffic, including database and schema compatibility checks.

Backup, restore, and disaster recovery

A backup is not considered trustworthy merely because pg_dump exits successfully.

The recovery flow verifies the restored state:

PostgreSQL source
      ↓
consistent backup snapshot
      ↓
dump + integrity manifest
      ↓
restore into disposable database
      ↓
full-state verification
      ↓
remove derived projections
      ↓
rebuild projections
      ↓
canonical audit

The manifest fingerprints:

table contents,

schema objects,

sequence state,

Alembic revision,

canonical truth excluding rebuildable projections.

The restore drill has been executed successfully against a real PostgreSQL database.

Example verified result:

restore_exact_match=true
projection_rebuild_verified=true
canonical_audit_ok=true

Deployment correctness

The application does not automatically mutate the database schema during startup.

A release must satisfy deployment gates before it is considered ready:

Application release
      ↓
Database reachable?
      ↓
Database schema == expected Alembic head?
      ↓
Canonical audit valid?
      ↓
Process serving
      ↓
Readiness + smoke verification

The deployment layer includes:

startup readiness gates,

Alembic revision compatibility checks,

release metadata,

pre-deployment verification,

real-process smoke tests,

graceful shutdown behavior.

A schema mismatch fails closed rather than allowing a new application version to run against an incompatible database.

Correctness model

The V1 architecture closes four major correctness layers:

System correctness
├── Business correctness
├── State correctness
├── Security correctness
└── Operational correctness
    ├── Observability
    ├── Backup / Restore
    └── Deployment / Failure Operations

The design process used throughout the project is:

Business objective
      ↓
Desired behavior
      ↓
Invariant
      ↓
Failure mode
      ↓
Prevent / contain / detect / recover / explain
      ↓
Automated proof

Complexity is added only when a concrete failure mode justifies it.

Verification

Current V1 verification:

179 automated tests passing

The suite covers areas including:

accounts and ownership,

canonical events,

transfers,

credit-card semantics,

refunds and reversals,

reconciliation,

concurrency,

idempotency,

provider evidence,

provider interpretation,

provider lifecycle,

crash-safe provider synchronization,

projections,

security,

observability,

disaster recovery,

deployment correctness,

end-to-end V1 acceptance scenarios.

In addition to automated tests, the project has completed:

a real PostgreSQL backup/restore drill,

projection deletion and rebuild verification,

canonical-state audit after restore,

deployment preflight,

real Uvicorn process smoke testing,

liveness and readiness verification,

SIGTERM shutdown testing.

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

The architecture is intentionally a modular monolith.

Microservices, Kafka, Redis, Kubernetes, and other infrastructure are not added unless a real failure mode or deployment requirement justifies them.

Running locally

From the backend directory, configure a PostgreSQL database through DATABASE_URL.

Run the test suite:

DATABASE_URL=postgresql://USER:PASSWORD@localhost:5432/finance_test_db \
pytest -q

Run the API:

uvicorn app.main:app --reload

Run deployment preflight:

DATABASE_URL=postgresql://USER:PASSWORD@localhost:5432/finance_test_db \
python -m scripts.deployment_preflight

Run a deployment smoke test against a running instance:

python -m scripts.deployment_smoke \
  --base-url http://127.0.0.1:8000

See the backend documentation for security, observability, disaster recovery, and deployment details.

Current scope and next steps

V1 closes the correctness model under the currently modeled environment.

The next phase is about connecting the system to production reality rather than adding another abstract correctness layer:

real financial-provider integration,

frontend product experience,

real deployment topology,

shared rate limiting,

PostgreSQL WAL archiving and point-in-time recovery,

production monitoring/alerting stack,

CI/CD automation.

These additions will introduce new real-world failure modes. The same design process will be applied to them:

new reality
→ new failure mode
→ invariant
→ justified mechanism
→ verification

Project philosophy

The project is built around a small set of principles:

Do not modify evidence to make state look correct.

Derived state must be rebuildable from canonical truth.

Uncertainty should remain explicit.

Failure modes justify mechanisms.

Simplicity is a correctness strategy.

When expected state and actual state diverge, find the first point of divergence.

The result is intentionally not a feature-heavy finance application. It is a backend engineering project focused on making financial state explainable, auditable, recoverable, and difficult to corrupt silently.