from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import (
    FinancialAccount,
    FinancialEvent,
    FinancialEventEntry,
    Transaction,
)


@dataclass(frozen=True)
class FinancialSummary:
    total_income: Decimal
    total_expense: Decimal
    balance: Decimal


def _signed_amount(transaction: Transaction) -> Decimal:
    amount = Decimal(transaction.amount)
    if amount <= 0:
        raise ValueError("Legacy transaction amount must be positive")

    if transaction.type == "income":
        return amount
    if transaction.type == "expense":
        return -amount
    raise ValueError(f"Unsupported legacy transaction type: {transaction.type}")


def _validate_transaction_ownership(db: Session, transaction: Transaction) -> None:
    account = (
        db.query(FinancialAccount)
        .filter(FinancialAccount.id == transaction.account_id)
        .one_or_none()
    )
    if account is None:
        raise ValueError("Transaction account does not exist")
    if account.user_id != transaction.user_id:
        raise ValueError("Transaction owner does not match account owner")


def sync_canonical_from_legacy_transaction(
    db: Session,
    transaction: Transaction,
) -> FinancialEvent:
    """Mirror one legacy transaction into the canonical model.

    Task 2 intentionally keeps Transaction as the active API contract while
    FinancialEvent + FinancialEventEntry run in parallel. This function is
    idempotent for a given legacy transaction and must be called before the
    surrounding DB transaction commits.
    """

    if transaction.id is None:
        raise ValueError("Legacy transaction must be flushed before canonical sync")
    if transaction.date is None:
        raise ValueError("Legacy transaction occurrence time is missing")

    _validate_transaction_ownership(db, transaction)
    signed_amount = _signed_amount(transaction)

    event = (
        db.query(FinancialEvent)
        .filter(FinancialEvent.legacy_transaction_id == transaction.id)
        .one_or_none()
    )

    if event is None:
        event = FinancialEvent(
            user_id=transaction.user_id,
            legacy_transaction_id=transaction.id,
            event_type=transaction.type.upper(),
            description=transaction.description,
            category=transaction.category,
            occurred_at=transaction.date,
            effective_at=transaction.date,
            interpretation_state="USER_CONFIRMED",
            provenance="USER_MANUAL",
            confidence="USER_CONFIRMED",
        )
        db.add(event)
        db.flush()
    else:
        if event.user_id != transaction.user_id:
            raise ValueError("Canonical event owner diverged from legacy owner")

        event.event_type = transaction.type.upper()
        event.description = transaction.description
        event.category = transaction.category
        event.occurred_at = transaction.date
        event.effective_at = transaction.date

    entries = (
        db.query(FinancialEventEntry)
        .filter(FinancialEventEntry.financial_event_id == event.id)
        .all()
    )
    if len(entries) > 1:
        raise ValueError(
            "Task 2 legacy mirror must contain exactly one canonical entry"
        )

    if entries:
        entry = entries[0]
        entry.account_id = transaction.account_id
        entry.amount = signed_amount
    else:
        db.add(
            FinancialEventEntry(
                financial_event_id=event.id,
                account_id=transaction.account_id,
                amount=signed_amount,
            )
        )

    return event


def delete_canonical_for_legacy_transaction(
    db: Session,
    legacy_transaction_id: int,
) -> None:
    """Temporary Task 2 compatibility for legacy hard-delete behavior.

    Canonical destructive deletion is not the final V1 semantics. It exists
    only while the legacy CRUD API remains authoritative. A later milestone
    replaces this with correction/history semantics.
    """

    event = (
        db.query(FinancialEvent)
        .filter(FinancialEvent.legacy_transaction_id == legacy_transaction_id)
        .one_or_none()
    )
    if event is None:
        return

    db.query(FinancialEventEntry).filter(
        FinancialEventEntry.financial_event_id == event.id
    ).delete(synchronize_session=False)
    db.delete(event)


def legacy_summary(db: Session, user_id: int) -> FinancialSummary:
    rows = db.query(Transaction).filter(Transaction.user_id == user_id).all()

    income = Decimal("0")
    expense = Decimal("0")
    for row in rows:
        amount = Decimal(row.amount)
        if row.type == "income":
            income += amount
        elif row.type == "expense":
            expense += amount

    return FinancialSummary(
        total_income=income,
        total_expense=expense,
        balance=income - expense,
    )


def canonical_summary(db: Session, user_id: int) -> FinancialSummary:
    income_value = (
        db.query(func.coalesce(func.sum(FinancialEventEntry.amount), 0))
        .join(
            FinancialEvent,
            FinancialEvent.id == FinancialEventEntry.financial_event_id,
        )
        .filter(
            FinancialEvent.user_id == user_id,
            FinancialEvent.event_type == "INCOME",
        )
        .scalar()
    )
    expense_signed_value = (
        db.query(func.coalesce(func.sum(FinancialEventEntry.amount), 0))
        .join(
            FinancialEvent,
            FinancialEvent.id == FinancialEventEntry.financial_event_id,
        )
        .filter(
            FinancialEvent.user_id == user_id,
            FinancialEvent.event_type == "EXPENSE",
        )
        .scalar()
    )

    income = Decimal(income_value or 0)
    expense = -Decimal(expense_signed_value or 0)
    return FinancialSummary(
        total_income=income,
        total_expense=expense,
        balance=income - expense,
    )
