# System Design

## Architecture

API Router
→ Service Layer
→ Repository / Persistence
→ PostgreSQL

Cross-cutting concerns:

- Authentication
- Authorization
- Validation
- Error handling
- Logging

## Design Principles

- Keep the system simple.
- Prefer a modular monolith.
- Business logic should not depend directly on HTTP.
- Database access should be separated from request handling.
- Failures should produce predictable behavior.
- Important invariants should be enforced by code and tests.

## Core Invariants

See `PRODUCT.md`.

## Failure Handling

Each important failure mode should map to:

Failure
→ Expected behavior
→ Mechanism
→ Test
## Failure Matrix

| Failure | Expected Behavior | Mechanism | Test |
|---|---|---|---|
| Invalid token | Reject request | Authentication dependency | Auth test |
| User accesses another user's transaction | Reject | Ownership check | Authorization test |
| Duplicate create retry | One transaction only | Idempotency key | Retry test |
| Invalid amount | Reject | Pydantic/domain validation | Validation test |
| Missing transaction | 404 | Domain error | API test |
| DB write fails | No partial state | Transaction rollback | Persistence test |