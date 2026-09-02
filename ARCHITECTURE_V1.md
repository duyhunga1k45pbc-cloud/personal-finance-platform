Personal Finance Platform — V1 Architecture & Domain Contract

Status: FROZEN FOR V1 IMPLEMENTATION
Purpose: define the business semantics, canonical state, invariants, state transitions, persistence model, API boundaries, concurrency rules, and migration plan before implementation.

1. Product Goal

The platform helps a user understand their real personal financial state across multiple sources:

bank accounts,

e-wallets,

credit cards,

cash,

manual records.

The system is not only a transaction CRUD application. Its core responsibility is:

Maintain a correct, explainable, reproducible financial state from imperfect and changing evidence.

The system must be able to answer:

What financial event happened?

Which account state changed?

When did it happen?

Where did the evidence come from?

How certain are we about the interpretation?

Does the current canonical state match external reality?

If not, where did the first divergence occur?

2. V1 Scope

Included

Multi-user system.

VND only.

Financial account types:

BANK

EWALLET

CREDIT_CARD

CASH

Manual financial-event recording.

Architecture ready for provider synchronization.

Immutable raw provider evidence.

Canonical financial-event interpretation.

Income / expense / transfer / refund / reversal / adjustment semantics.

Cash as a partially observable account.

User-driven classification of uncertain cash differences.

Append-only state/history records for significant transitions.

Deduplication and idempotency.

Concurrency protection.

Account reconciliation.

Derived reports/projections.

User data isolation.

Explicitly out of V1

Multi-currency / FX accounting.

Tax accounting.

Investment portfolio accounting.

Crypto accounting.

Full accounting ledger / double-entry general ledger.

Kafka / microservices.

Event-sourcing framework.

CQRS framework.

Automatic silent balance adjustments.

These may be introduced only when a concrete business requirement or failure mode justifies them.

3. Core Architecture Principle

The system follows this chain:

External Reality
      ↓
Raw Evidence
      ↓
Identity / Deduplication
      ↓
Normalization
      ↓
Canonical Interpretation
      ↓
Validated State Transition
      ↓
Canonical Financial State
      ↓
Reconciliation + Projections
      ↓
Dashboard / Reports / Audit

Cross-cutting dimensions:

Identity
Ownership
Time
Provenance
Confidence
Causality
History
Concurrency

4. Truth Hierarchy

V1 distinguishes different kinds of truth.

4.1 Raw Evidence Truth

What an external provider or user actually reported.

Examples:

provider transaction payload,

provider balance observation,

manual user entry.

Raw provider evidence is immutable.

4.2 Canonical Interpretation

The system's current best interpretation of an economic event.

Examples:

ATM WITHDRAWAL -2,000,000 interpreted as TRANSFER Bank → Cash,

credit-card purchase interpreted as EXPENSE,

duplicate provider record recognized as the same economic event.

Canonical interpretation may be corrected, but corrections must preserve history.

4.3 Canonical Financial State

The current financial state derived from canonical events.

Examples:

account expected balance,

credit-card liability,

expected cash,

net worth.

4.4 Observed External State

What a provider currently reports.

Example:

Expected bank balance: 20,000,000
Observed bank balance: 19,800,000

The system must not silently force them to agree.

4.5 Projection

Read models derived from canonical state/events:

total income,

total expense,

cash flow,

spending by category,

net worth,

dashboard summaries.

Projections are not canonical truth and must be rebuildable.

5. Domain Entities

User
 ├── FinancialAccount
 ├── ProviderConnection
 ├── ExternalTransaction
 ├── FinancialEvent
 │     ├── FinancialEventEntry
 │     ├── FinancialEventLink
 │     └── FinancialEventHistory
 ├── Reconciliation
 └── IdempotencyRecord

5.1 User

Owns all financial data in a strict user boundary.

5.2 FinancialAccount

Represents a financial container owned by a user.

Types:

BANK
EWALLET
CREDIT_CARD
CASH

5.3 ProviderConnection

Represents a connection to an external data source.

Examples:

a bank integration,

an e-wallet integration,

a credit-card provider integration.

5.4 ExternalTransaction

Immutable evidence received from a provider.

It is not automatically the same thing as the canonical economic event.

5.5 FinancialEvent

Canonical interpretation of an economic event.

Types:

INCOME
EXPENSE
TRANSFER
REFUND
REVERSAL
ADJUSTMENT

5.6 FinancialEventEntry

Represents the account-level effect of a financial event.

This exists separately from FinancialEvent because one event may affect multiple accounts.

Example transfer:

TRANSFER #T1

Entry 1: VCB   -2,000,000
Entry 2: MoMo  +2,000,000

5.7 FinancialEventLink

Represents causality between events.

V1 relation types:

TRANSFER_COUNTERPART
REFUND_OF
REVERSAL_OF
FEE_FOR

5.8 FinancialEventHistory

Append-only history of significant state or interpretation changes.

5.9 Reconciliation

Compares expected canonical state against observed external state.

5.10 IdempotencyRecord

Protects user/API commands from repeated execution.

6. Time Model

Time is a first-class dimension.

6.1 occurred_at

When the economic event happened in reality.

6.2 observed_at

When a provider/user reported the event/state.

6.3 recorded_at

When this system persisted the evidence/event.

6.4 effective_at

When the event becomes effective in canonical financial state.

Example:

31 Aug 20:00 — card purchase occurs
31 Aug 20:01 — provider reports PENDING
01 Sep 08:00 — provider reports POSTED
01 Sep 08:01 — system records update

This allows the system to distinguish:

What actually happened when?

from:

What did the system know at time T?

7. Provenance and Confidence

Provenance

PROVIDER
USER_MANUAL
SYSTEM_INFERRED

Confidence / observation type

OBSERVED
INFERRED
USER_CONFIRMED

Example:

ATM withdrawal from bank  → OBSERVED
Cash +2m                  → INFERRED
User confirms cash count  → USER_CONFIRMED

Correctness means the system does not pretend inferred state is directly observed state.

8. Economic Event Semantics

8.1 INCOME

Represents value entering the user's economic state.

Example:

Salary received into Bank A
Bank A +20,000,000
Income +20,000,000
Net worth +20,000,000

8.2 EXPENSE

Represents consumed value.

Example:

Food paid from Bank A
Bank A -100,000
Expense +100,000
Net worth -100,000

8.3 TRANSFER

Movement between accounts owned by the same user.

Example:

VCB → MoMo: 2,000,000

VCB  -2,000,000
MoMo +2,000,000
Income delta  = 0
Expense delta = 0
Net-worth delta = 0

ATM withdrawal

Bank → Cash

Not an expense.

Transfer fee

A fee is a separate EXPENSE linked with FEE_FOR.

Example:

TRANSFER: Bank → Cash 2,000,000
EXPENSE:  Banking fee 11,000

8.4 CREDIT CARD PURCHASE

A credit-card purchase is represented as EXPENSE against a CREDIT_CARD account.

Expense is recognized when the purchase becomes POSTED, not when the card bill is paid.

Example:

Purchase: 20,000,000

Expense +20,000,000
Credit-card liability +20,000,000
Net worth -20,000,000

Card repayment:

Bank -20,000,000
Credit-card liability -20,000,000
Expense delta = 0
Net-worth delta = 0

A credit-card repayment is a TRANSFER, not an expense.

8.5 REFUND

A refund means the original purchase really occurred, then a later economic event compensated part or all of it.

Rules:

create a new event,

link with REFUND_OF,

never mutate the original purchase amount/history.

Example:

Purchase: 20m
Refund:    5m
Effective expense: 15m

8.6 REVERSAL

A reversal means an original transaction is effectively cancelled or undone.

Difference from refund:

REFUND
Original economic event happened, then compensation happened later.

REVERSAL
Original event is cancelled/invalidated as an effective economic event.

Rules:

create or record a reversal transition/event,

link with REVERSAL_OF,

never delete the original evidence/event.

8.7 ADJUSTMENT

Adjustment is a last-resort reconciliation mechanism.

It must not become a generic "make balances match" mechanism.

Allowed example:

Expected cash: 1,500,000
Actual cash:   1,300,000
Difference:     -200,000

If the user knows where the money went:

Food    -150,000
Parking  -50,000

create normal EXPENSE events.

If the user cannot determine the cause:

ADJUSTMENT -200,000
reason = CASH_RECONCILIATION_UNKNOWN

Policy:

System detects/proposes mismatch.
User explicitly confirms adjustment.
System never silently creates financial adjustments.

9. Cash Model

Cash is partially observable.

Bank-visible cash transfer

ATM withdrawal 2,000,000
Bank -2,000,000
Cash +2,000,000

Unknown cash spending

If the user spends cash without recording it, the platform cannot observe the economic event.

Therefore:

Expected cash
!=
Actual physical cash

is a valid state.

The system must surface the mismatch rather than fake precision.

Workflow:

Expected Cash
     ↓
User counts Actual Cash
     ↓
Mismatch detected
     ↓
User classifies missing spending
     ↓
or explicitly confirms Adjustment
     ↓
Reconciliation resolved

10. Exact Enums

account_type

BANK
EWALLET
CREDIT_CARD
CASH

currency_code

VND

financial_event_type

INCOME
EXPENSE
TRANSFER
REFUND
REVERSAL
ADJUSTMENT

source_transaction_state

PENDING
POSTED
REVERSED

interpretation_state

UNCLASSIFIED
CLASSIFIED
USER_CONFIRMED

reconciliation_state

UNKNOWN
RECONCILED
MISMATCH
RESOLVED

provenance_type

PROVIDER
USER_MANUAL
SYSTEM_INFERRED

confidence_type

OBSERVED
INFERRED
USER_CONFIRMED

event_relation_type

TRANSFER_COUNTERPART
REFUND_OF
REVERSAL_OF
FEE_FOR

actor_type

USER
SYSTEM
PROVIDER
AI

11. PostgreSQL Table Contract

The following schema is conceptual V1 contract. Exact SQLAlchemy implementation may vary, but semantics and constraints must remain equivalent.

11.1 users

Existing user table remains the ownership root.

Required fields:

id
email
hashed_password
created_at

11.2 financial_accounts

id                    UUID / PK
user_id               FK users / NOT NULL
name                  NOT NULL
account_type          account_type / NOT NULL
currency              currency_code / NOT NULL / DEFAULT VND
provider_name         nullable
external_account_id   nullable
version               BIGINT NOT NULL DEFAULT 1
created_at            TIMESTAMPTZ NOT NULL
updated_at            TIMESTAMPTZ NOT NULL

Constraints:

account belongs to exactly one user
currency must be VND in V1

Recommended indexes:

(user_id)
(user_id, account_type)

11.3 provider_connections

id
user_id
provider_name
status
last_synced_at
created_at
updated_at

Provider credentials/tokens must not be stored in plaintext.

11.4 external_transactions

Immutable provider evidence.

id
user_id
provider_connection_id
external_transaction_id
amount                  NUMERIC(18,2)
currency                VND
source_state
provider_description
occurred_at
observed_at
recorded_at
raw_payload              JSONB
created_at

Hard constraint:

UNIQUE(provider_connection_id, external_transaction_id)

User API must not expose destructive DELETE for this table.

11.5 financial_events

id
user_id
event_type
interpretation_state
provenance
confidence
occurred_at
effective_at
recorded_at
version                  BIGINT NOT NULL DEFAULT 1
created_at
updated_at

Optional external evidence mapping may be represented by a dedicated link table rather than a single foreign key so one event can reference multiple evidence records later.

11.6 financial_event_entries

id
financial_event_id
account_id
amount                   NUMERIC(18,2)
created_at

Recommended indexes:

(financial_event_id)
(account_id)

11.7 financial_event_evidence

Links canonical interpretation to source evidence.

financial_event_id
external_transaction_id

This makes provenance traceable without forcing 1:1 mapping.

11.8 financial_event_links

id
from_event_id
to_event_id
relation_type
created_at

Examples:

refund → REFUND_OF → purchase
reversal → REVERSAL_OF → original
fee → FEE_FOR → transfer

11.9 financial_event_history

Append-only audit/state history.

id
financial_event_id
event_name
previous_state          JSONB nullable
new_state               JSONB
actor_type
actor_user_id           nullable
request_id              nullable
metadata                JSONB nullable
occurred_at	recorded_at
created_at

Rules:

No UPDATE of old history rows.
No normal DELETE of history rows.
Correction creates a new history event.

State change and history append must occur in the same database transaction.

11.10 reconciliations

id
user_id
account_id
expected_balance        NUMERIC(18,2)
observed_balance        NUMERIC(18,2)
difference              NUMERIC(18,2)
state
observed_at
recorded_at
resolved_at             nullable
resolution_event_id     nullable
created_at

11.11 idempotency_records

id
user_id
idempotency_key
command_type
request_hash
result_reference
created_at
expires_at              nullable

Constraint:

UNIQUE(user_id, idempotency_key)

12. State Machines

12.1 Provider Transaction State

            ┌───────────┐
            │  PENDING  │
            └─────┬─────┘
                  │
          ┌───────┴────────┐
          ▼                ▼
       POSTED           REVERSED
          │
          ▼
       REVERSED

Allowed matrix:

Current

Next

Allowed

none

PENDING

yes

none

POSTED

yes

PENDING

POSTED

yes

PENDING

REVERSED

yes

POSTED

REVERSED

yes

REVERSED

PENDING

no

REVERSED

POSTED

no by default

Unexpected provider transitions must be preserved as evidence and flagged, not silently normalized away.

12.2 Interpretation State

UNCLASSIFIED
   ├── system classification ──→ CLASSIFIED
   └── user classification ────→ USER_CONFIRMED

CLASSIFIED
   ├── user confirms ──────────→ USER_CONFIRMED
   └── user changes ───────────→ USER_CONFIRMED

USER_CONFIRMED
   ├── user correction ────────→ USER_CONFIRMED + history
   └── system/AI overwrite ────→ FORBIDDEN

A machine classifier must never silently overwrite a user-confirmed interpretation.

12.3 Reconciliation State

UNKNOWN
   ├── matches reality ──→ RECONCILED
   └── mismatch ─────────→ MISMATCH

RECONCILED
   └── later mismatch ───→ MISMATCH

MISMATCH
   ├── source resolves ──→ RESOLVED
   ├── user classifies ──→ RESOLVED
   └── adjustment ───────→ RESOLVED

RESOLVED
   └── next reconciliation cycle evaluates again

RESOLVED records that a mismatch was handled; it does not mean future observations cannot diverge again.

13. Core Invariants

Evidence and History

INV-001 — Raw provider evidence is immutable.

INV-002 — A provider transaction may be received multiple times but must have one stable external identity within its provider connection.

INV-003 — Significant canonical state changes must append history.

INV-004 — State mutation and corresponding history append must commit atomically.

Ownership

INV-005 — Every financial account belongs to exactly one user.

INV-006 — Every canonical event belongs to exactly one user.

INV-007 — An event may affect only accounts owned by the same event owner.

INV-008 — User A must never read or mutate User B's financial state.

Economic Semantics

INV-009 — Internal transfer between accounts of the same user does not change net worth.

INV-010 — Internal transfer does not create income or expense.

INV-011 — Credit-card repayment is not an expense.

INV-012 — Credit-card expense is recognized on posted purchase, not repayment.

INV-013 — Refund creates a new linked event and does not rewrite the original purchase.

INV-014 — Reversal preserves original evidence/history and cancels effective economic impact according to business rules.

INV-015 — Transfer fees are separate expenses linked to the transfer.

Cash and Reconciliation

INV-016 — Cash may be inferred and may legitimately differ from actual physical cash until reconciliation.

INV-017 — Source conflict must remain explicit until resolved.

INV-018 — The system must not silently create adjustments to force expected and observed balances to agree.

INV-019 — User confirmation is required for unknown-cause adjustments in V1.

Correctness / Concurrency

INV-020 — Repeated execution of the same idempotent command must not create duplicate economic effects.

INV-021 — Concurrent mutations must not silently overwrite newer state.

INV-022 — Invalid state-machine transitions must be rejected or quarantined for investigation.

Projection

INV-023 — Reports and summaries are derived state, not canonical truth.

INV-024 — Derived projections must be rebuildable from canonical events/state.

INV-025 — A dashboard number must be traceable to canonical events and, where applicable, source evidence.

14. Duplicate and Idempotency Policy

Provider ingestion

Stable identity:

(provider_connection_id, external_transaction_id)

The database unique constraint is the final protection against duplicate provider evidence.

Repeated sync must be safe.

User/API commands

Mutation commands should accept an idempotency key where duplicate execution could create duplicate economic effects.

Example:

POST create manual event
client times out after commit
client retries same idempotency key
→ return/reuse previous result
→ do not create a second event

15. Concurrency Policy

V1 uses PostgreSQL transactions plus explicit concurrency control.

Optimistic locking

Mutable canonical aggregates use a version field.

Conceptual update:

UPDATE financial_events
SET interpretation_state = :new_state,
    version = version + 1
WHERE id = :id
  AND version = :expected_version;

If affected rows = 0:

state changed concurrently
→ reload
→ re-evaluate business transition
→ retry or reject

Row locking

Use transaction-level row locks only when a business operation requires coordinated mutation of shared state.

Example:

Transfer A → B

The event creation, entries, invariant validation, and history append must commit atomically.

User-confirmed interpretation priority

Background sync/classification must not overwrite newer user-confirmed state.

16. Reconciliation Model

Reconciliation compares:

Expected state from canonical events
vs
Observed state from provider/user

Example:

Expected bank balance = 20,000,000
Observed bank balance = 19,800,000
Difference            =   -200,000
State                 = MISMATCH

The system must preserve all three values.

It must not rewrite canonical history merely to make the numbers equal.

Investigation may reveal:

missing provider transaction
duplicate transaction
pending/posted identity mismatch
provider delay
incorrect transfer interpretation
incorrect classification
unknown cash activity

The debugging principle is:

Find the first point where expected state and actual/observed state diverged.

17. Command / Query Boundary

The application should no longer be designed as arbitrary ORM CRUD.

Commands — mutate state

Examples:

CreateManualFinancialEvent
ClassifyFinancialEvent
ReclassifyFinancialEvent
CreateTransfer
RecordRefund
RecordReversal
ConfirmCashBalance
ResolveCashDifference
ConfirmAdjustment
SyncProviderTransactions
ReconcileAccount

Command flow:

API
 ↓
Command
 ↓
Application Service
 ↓
Load current state
 ↓
Validate ownership + transition + invariant
 ↓
Apply atomic state change
 ↓
Append history
 ↓
Commit

Queries — read state

Examples:

GetAccounts
GetFinancialEvents
GetFinancialEventDetail
GetFinancialEventHistory
GetUnclassifiedEvents
GetAccountBalance
GetCashFlow
GetNetWorth
GetReconciliationStatus
GetMismatches
GetSummary

Queries must not mutate canonical state.

18. API Direction

Exact HTTP routes may evolve, but the API should express business operations rather than generic table mutation.

Possible V1 direction:

POST   /accounts
GET    /accounts
GET    /accounts/{id}

POST   /events/manual
GET    /events
GET    /events/{id}
GET    /events/{id}/history

POST   /events/{id}/classification
POST   /events/{id}/correction

POST   /transfers
POST   /refunds
POST   /reversals

POST   /accounts/{id}/cash-reconciliation
GET    /accounts/{id}/reconciliation

GET    /summary
GET    /net-worth
GET    /cash-flow

Avoid exposing raw PUT financial_event semantics that allow arbitrary state mutation.

19. Current Repository → V1 Migration Plan

Current repository state on main is intentionally simple:

User
Transaction
Summary

Current Transaction contains:

id
amount: Float
description
category
date
type: income | expense
user_id

Migration must be incremental. Do not rewrite everything at once.

Phase A — Introduce FinancialAccount

Add financial_accounts.

For each existing user create:

Default Cash
account_type = CASH
currency = VND

Existing behavior remains functional.

Phase B — Introduce Canonical FinancialEvent + Entries

Map old transactions:

old income X
→ FinancialEvent(INCOME)
→ Default Cash +X

old expense X
→ FinancialEvent(EXPENSE)
→ Default Cash -X

Use NUMERIC, not Float, for new money columns.

Phase C — Introduce History

All new significant mutations append financial_event_history.

Do not yet require full provider integration.

Phase D — Replace Destructive Canonical CRUD

Move away from arbitrary update/delete semantics.

old arbitrary UPDATE
→ business correction/reclassification command

old DELETE
→ explicit correction/reversal semantics depending on event origin

Manual mistaken records may use a correction policy, but provider raw evidence remains immutable.

Phase E — Add Provider Evidence Layer

Introduce:

provider_connections
external_transactions
financial_event_evidence

Implement dedup and pending/posted/reversed state handling.

Phase F — Implement Transfer + Credit-Card Semantics

Implement:

TRANSFER
credit-card purchase
credit-card repayment
FEE_FOR

Phase G — Implement Reconciliation

Add expected vs observed account state.

Implement explicit MISMATCH handling.

Phase H — Rebuild Summary as Projection

Existing /summary semantics should be reimplemented on top of canonical financial events.

Correctness test:

Delete/rebuild derived projection
→ result must equal previous correct projection

20. Recommended Modular-Monolith Structure

V1 should remain a modular monolith.

backend/app/
├── api/
│   ├── auth.py
│   ├── accounts.py
│   ├── events.py
│   └── reconciliation.py
│
├── application/
│   ├── commands/
│   ├── queries/
│   └── services/
│
├── domain/
│   ├── accounts/
│   ├── financial_events/
│   ├── reconciliation/
│   └── invariants/
│
├── infrastructure/
│   ├── db/
│   ├── repositories/
│   └── providers/
│
├── projections/
│   ├── summary.py
│   └── net_worth.py
│
└── auth/

This structure is a direction, not a requirement to reorganize the entire repository in one commit.

Migration should preserve working behavior while boundaries are introduced gradually.

21. Implementation Rules for AI Coding Agents

AI is an implementation accelerator, not the owner of domain semantics.

Before assigning an implementation task, specify:

business objective
allowed state transitions
invariants
failure modes
transaction boundary
acceptance tests

Example task:

Implement FinancialAccount only.

Preserve existing authentication and transaction behavior.
Create one default CASH account for existing users.
Currency must be VND.
Enforce user ownership.
Use NUMERIC for monetary values.
Do not implement provider sync, reconciliation, transfer, or event sourcing.

AI must not introduce architectural mechanisms without a demonstrated requirement.

22. Correctness Test Strategy

Tests should be expressed as state-transition/invariant tests, not only HTTP status tests.

Pattern:

Given State₀
When Command X
Then State₁
And INV-xxx still holds

Mandatory examples:

User A cannot access User B account.             INV-008
Transfer preserves net worth.                    INV-009
Transfer creates no income/expense.              INV-010
Credit-card repayment creates no expense.        INV-011
Refund preserves original purchase history.      INV-013
Concurrent correction does not silently overwrite. INV-021
Repeated idempotent command creates one effect.  INV-020
Projection rebuild reproduces summary.           INV-024
Source mismatch remains explicit.                INV-017
User-confirmed classification survives AI rerun.

23. Definition of V1 Architectural Success

V1 architecture is successful if the platform can satisfy all of these statements:

Evidence is immutable.
Interpretation is correctable and auditable.
State is reproducible.
Ownership is explicit.
Time is explicit.
Uncertainty is explicit.
Conflict is explicit.
Duplicate effects are prevented.
Concurrent writes cannot silently corrupt state.
Corrections preserve history.
Projections are derived.
Every important result is traceable.

The goal is not maximum feature count.

The goal is:

A small personal-finance system whose financial state is difficult to corrupt silently and easy to explain when reality and expected state diverge.

24. Architecture Freeze Rule

After this document is accepted, V1 domain semantics are considered frozen.

A new mechanism should be added only when at least one of the following exists:

a new explicit business requirement,

a demonstrated failure mode,

an invariant that cannot be maintained with the current mechanism.

Do not add infrastructure or abstractions merely because they are common, modern, or technically interesting.

25. First Implementation Milestone

The first implementation milestone is intentionally small:

FinancialAccount
+ VND-only contract
+ user ownership
+ default CASH account migration
+ tests for ownership/isolation

Do not start provider synchronization until the internal canonical model and ownership rules are stable.

