from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.credit_card_service import validate_transaction_account_semantics
from app.models import (
    FinancialAccount,
    FinancialEvent,
    FinancialEventEntry,
    FinancialEventHistory,
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


def _validate_transaction_ownership(
    db: Session, transaction: Transaction
) -> FinancialAccount:
    account = (
        db.query(FinancialAccount)
        .filter(FinancialAccount.id == transaction.account_id)
        .one_or_none()
    )
    if account is None:
        raise ValueError("Transaction account does not exist")
    if account.user_id != transaction.user_id:
        raise ValueError("Transaction owner does not match account owner")
    validate_transaction_account_semantics(account, transaction.type)
    return account


def snapshot_financial_event(db: Session, event: FinancialEvent) -> dict:
    entries = (
        db.query(FinancialEventEntry)
        .filter(FinancialEventEntry.financial_event_id == event.id)
        .order_by(FinancialEventEntry.id.asc())
        .all()
    )
    return {
        "event_type": event.event_type,
        "description": event.description,
        "category": event.category,
        "occurred_at": event.occurred_at.isoformat() if event.occurred_at else None,
        "effective_at": event.effective_at.isoformat() if event.effective_at else None,
        "interpretation_state": event.interpretation_state,
        "provenance": event.provenance,
        "confidence": event.confidence,
        "lifecycle_state": event.lifecycle_state,
        "version": event.version,
        "entries": [
            {
                "account_id": entry.account_id,
                "amount": format(Decimal(entry.amount), "f"),
            }
            for entry in entries
        ],
    }


def append_financial_event_history(
    db: Session,
    event: FinancialEvent,
    *,
    transition_type: str,
    actor_type: str,
    actor_user_id: int | None,
    previous_state: dict | None,
    new_state: dict,
    reason: str | None = None,
) -> FinancialEventHistory:
    history = FinancialEventHistory(
        financial_event_id=event.id,
        user_id=event.user_id,
        event_version=event.version,
        transition_type=transition_type,
        actor_type=actor_type,
        actor_user_id=actor_user_id,
        previous_state=previous_state,
        new_state=new_state,
        reason=reason,
    )
    db.add(history)
    db.flush()
    return history


def sync_canonical_from_legacy_transaction(
    db: Session,
    transaction: Transaction,
    *,
    actor_type: str = "SYSTEM",
    actor_user_id: int | None = None,
    reason: str | None = None,
) -> FinancialEvent:
    """Mirror one legacy transaction into canonical state with history.

    Task 3 keeps the legacy Transaction API as a compatibility surface, but any
    canonical state mutation is now versioned and produces append-only history.
    Calling this function with an already-equal state is idempotent and does not
    create a new history row or bump the version.
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
            lifecycle_state="ACTIVE",
            version=1,
        )
        db.add(event)
        db.flush()
        db.add(
            FinancialEventEntry(
                financial_event_id=event.id,
                account_id=transaction.account_id,
                amount=signed_amount,
            )
        )
        db.flush()
        append_financial_event_history(
            db,
            event,
            transition_type="CREATED",
            actor_type=actor_type,
            actor_user_id=actor_user_id,
            previous_state=None,
            new_state=snapshot_financial_event(db, event),
            reason=reason,
        )
        return event

    if event.user_id != transaction.user_id:
        raise ValueError("Canonical event owner diverged from legacy owner")
    if event.lifecycle_state != "ACTIVE":
        raise ValueError("Cannot mutate a voided canonical event")

    previous_state = snapshot_financial_event(db, event)

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
    if len(entries) != 1:
        raise ValueError(
            "Legacy compatibility event must contain exactly one canonical entry"
        )

    entry = entries[0]
    entry.account_id = transaction.account_id
    entry.amount = signed_amount
    db.flush()

    candidate_state = snapshot_financial_event(db, event)
    if candidate_state == previous_state:
        return event

    event.version += 1
    db.flush()
    append_financial_event_history(
        db,
        event,
        transition_type="CORRECTED",
        actor_type=actor_type,
        actor_user_id=actor_user_id,
        previous_state=previous_state,
        new_state=snapshot_financial_event(db, event),
        reason=reason,
    )
    return event


def void_canonical_for_legacy_transaction(
    db: Session,
    legacy_transaction_id: int,
    *,
    actor_type: str = "SYSTEM",
    actor_user_id: int | None = None,
    reason: str | None = None,
) -> FinancialEvent:
    event = (
        db.query(FinancialEvent)
        .filter(FinancialEvent.legacy_transaction_id == legacy_transaction_id)
        .one_or_none()
    )
    if event is None:
        raise ValueError("Canonical event does not exist")
    if event.lifecycle_state == "VOIDED":
        return event

    previous_state = snapshot_financial_event(db, event)
    event.lifecycle_state = "VOIDED"
    event.version += 1
    db.flush()
    append_financial_event_history(
        db,
        event,
        transition_type="VOIDED",
        actor_type=actor_type,
        actor_user_id=actor_user_id,
        previous_state=previous_state,
        new_state=snapshot_financial_event(db, event),
        reason=reason,
    )
    return event


def legacy_summary(db: Session, user_id: int) -> FinancialSummary:
    rows = (
        db.query(Transaction)
        .join(
            FinancialEvent,
            FinancialEvent.legacy_transaction_id == Transaction.id,
        )
        .filter(
            Transaction.user_id == user_id,
            FinancialEvent.lifecycle_state == "ACTIVE",
        )
        .all()
    )

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
            FinancialEvent.lifecycle_state == "ACTIVE",
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
            FinancialEvent.lifecycle_state == "ACTIVE",
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


class ConcurrentModificationError(RuntimeError):
    def __init__(self, *, expected_version: int, current_version: int | None):
        self.expected_version = expected_version
        self.current_version = current_version
        super().__init__(
            f"Stale financial event version: expected {expected_version}, "
            f"current {current_version}"
        )


def _claim_expected_version(
    db: Session,
    event: FinancialEvent,
    *,
    expected_version: int,
) -> None:
    """Atomically verify the expected version and serialize the mutation.

    The no-op UPDATE is intentional. PostgreSQL rechecks the version predicate
    after waiting on a concurrent writer, so only one writer can claim a given
    version. A stale writer gets rowcount=0 instead of silently overwriting a
    newer state.
    """

    claimed = (
        db.query(FinancialEvent)
        .filter(
            FinancialEvent.id == event.id,
            FinancialEvent.lifecycle_state == "ACTIVE",
            FinancialEvent.version == expected_version,
        )
        .update(
            {FinancialEvent.version: FinancialEvent.version},
            synchronize_session=False,
        )
    )
    if claimed == 1:
        db.refresh(event)
        return

    current_version = (
        db.query(FinancialEvent.version)
        .filter(FinancialEvent.id == event.id)
        .scalar()
    )
    raise ConcurrentModificationError(
        expected_version=expected_version,
        current_version=current_version,
    )


def correct_legacy_transaction_with_expected_version(
    db: Session,
    transaction: Transaction,
    *,
    amount: Decimal,
    description: str,
    category: str,
    transaction_type: str,
    account_id: int,
    expected_version: int,
    actor_type: str = "USER",
    actor_user_id: int | None = None,
    reason: str | None = None,
) -> FinancialEvent:
    if expected_version < 1:
        raise ValueError("Expected version must be positive")
    if amount <= 0:
        raise ValueError("Transaction amount must be positive")
    if transaction_type not in {"income", "expense"}:
        raise ValueError("Unsupported transaction type")

    account = (
        db.query(FinancialAccount)
        .filter(FinancialAccount.id == account_id)
        .one_or_none()
    )
    if account is None or account.user_id != transaction.user_id:
        raise ValueError("Transaction owner does not match account owner")
    validate_transaction_account_semantics(account, transaction_type)

    event = (
        db.query(FinancialEvent)
        .filter(FinancialEvent.legacy_transaction_id == transaction.id)
        .one_or_none()
    )
    if event is None:
        raise ValueError("Canonical event does not exist")
    if event.user_id != transaction.user_id:
        raise ValueError("Canonical event owner diverged from legacy owner")

    _claim_expected_version(db, event, expected_version=expected_version)

    entries = (
        db.query(FinancialEventEntry)
        .filter(FinancialEventEntry.financial_event_id == event.id)
        .all()
    )
    if len(entries) != 1:
        raise ValueError(
            "Legacy compatibility event must contain exactly one canonical entry"
        )
    entry = entries[0]

    signed_amount = amount if transaction_type == "income" else -amount
    unchanged = (
        event.event_type == transaction_type.upper()
        and event.description == description
        and event.category == category
        and entry.account_id == account_id
        and Decimal(entry.amount) == signed_amount
    )
    if unchanged:
        return event

    previous_state = snapshot_financial_event(db, event)

    transaction.amount = amount
    transaction.description = description
    transaction.category = category
    transaction.type = transaction_type
    transaction.account_id = account_id

    event.event_type = transaction_type.upper()
    event.description = description
    event.category = category
    event.occurred_at = transaction.date
    event.effective_at = transaction.date
    entry.account_id = account_id
    entry.amount = signed_amount
    event.version = expected_version + 1

    db.flush()
    append_financial_event_history(
        db,
        event,
        transition_type="CORRECTED",
        actor_type=actor_type,
        actor_user_id=actor_user_id,
        previous_state=previous_state,
        new_state=snapshot_financial_event(db, event),
        reason=reason,
    )
    return event


def void_canonical_with_expected_version(
    db: Session,
    legacy_transaction_id: int,
    *,
    expected_version: int,
    actor_type: str = "USER",
    actor_user_id: int | None = None,
    reason: str | None = None,
) -> FinancialEvent:
    if expected_version < 1:
        raise ValueError("Expected version must be positive")

    event = (
        db.query(FinancialEvent)
        .filter(FinancialEvent.legacy_transaction_id == legacy_transaction_id)
        .one_or_none()
    )
    if event is None:
        raise ValueError("Canonical event does not exist")

    _claim_expected_version(db, event, expected_version=expected_version)

    previous_state = snapshot_financial_event(db, event)
    event.lifecycle_state = "VOIDED"
    event.version = expected_version + 1
    db.flush()
    append_financial_event_history(
        db,
        event,
        transition_type="VOIDED",
        actor_type=actor_type,
        actor_user_id=actor_user_id,
        previous_state=previous_state,
        new_state=snapshot_financial_event(db, event),
        reason=reason,
    )
    return event
