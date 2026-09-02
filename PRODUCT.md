# Personal Finance Platform

## 1. Problem

Users often record income and expenses inconsistently and lose visibility into their personal finances.

The system should provide a simple and reliable way to record, inspect, and summarize personal financial transactions.

## 2. Goal

Build a personal finance backend that allows authenticated users to:

- securely manage their own transactions
- categorize income and expenses
- filter and paginate transaction history
- view basic financial summaries
- trust that their data is isolated from other users

## 3. Users

Primary user:

- Individual managing personal income and expenses

## 4. Core Workflow

User registers
→ logs in
→ receives authentication token
→ creates transactions
→ views / filters / updates / deletes own transactions
→ views financial summary

## 5. Core Domain

### User

- email
- password
- owns transactions

### Transaction

- amount
- type: income / expense
- category
- date
- description
- owner

## 6. Constraints

- Each transaction belongs to exactly one user.
- A user must never access another user's transactions.
- Monetary values must be valid and positive.
- Protected operations require authentication.
- Database writes must not leave partial or inconsistent state.
- The system remains a modular monolith.

## 7. Failure Modes

### Authentication

- invalid credentials
- invalid or expired token
- duplicate account registration

### Authorization

- user attempts to read another user's transaction
- user attempts to update another user's transaction
- user attempts to delete another user's transaction

### Data Integrity

- invalid amount
- invalid transaction type
- transaction does not exist
- duplicate create request caused by retry

### Persistence

- database write fails
- request fails after partial processing

## 8. Invariants

- A user can access only resources they own.
- Transaction amount must be greater than zero.
- Every transaction must have a valid owner.
- Failed writes must not leave partial state.
- One logical idempotent create request must create at most one transaction.
- Financial summaries must include only the authenticated user's data.

## 9. Non-goals

For the current version:

- no bank synchronization
- no investment portfolio management
- no payment processing
- no microservices
- no complex distributed architecture

## 10. Success Criteria

The backend is considered reliable when:

- all core workflows are covered by automated tests
- authorization boundaries are tested
- retry scenarios do not create duplicate transactions
- Docker setup works from a clean environment
- CI passes
- important failures are observable through logs