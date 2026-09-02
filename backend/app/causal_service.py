from __future__ import annotations

import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.canonical_service import (
    append_financial_event_history,
    snapshot_financial_event,
)
from app.models import (
    FinancialAccount,
    FinancialEvent,
    FinancialEventEntry,
    FinancialEventLink,
)


class CausalEventError(ValueError):
    pass


def _normalize_occurred_at(value: datetime.datetime | None) -> datetime.datetime:
    if value is None:
        return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    if value.tzinfo is None:
        return value
    return value.astimezone(datetime.timezone.utc).replace(tzinfo=None)


def _owned_active_event(db: Session, *, user_id: int, event_id: int) -> FinancialEvent:
    event = (
        db.query(FinancialEvent)
        .filter(
            FinancialEvent.id == event_id,
            FinancialEvent.user_id == user_id,
            FinancialEvent.lifecycle_state == "ACTIVE",
        )
        .one_or_none()
    )
    if event is None:
        raise CausalEventError("Original financial event not found")
    return event


def _entries(db: Session, event_id: int) -> list[FinancialEventEntry]:
    return (
        db.query(FinancialEventEntry)
        .filter(FinancialEventEntry.financial_event_id == event_id)
        .order_by(FinancialEventEntry.id.asc())
        .all()
    )


def _active_children(
    db: Session,
    *,
    original_event_id: int,
    relation_type: str,
) -> list[FinancialEvent]:
    return (
        db.query(FinancialEvent)
        .join(
            FinancialEventLink,
            FinancialEventLink.from_event_id == FinancialEvent.id,
        )
        .filter(
            FinancialEventLink.to_event_id == original_event_id,
            FinancialEventLink.relation_type == relation_type,
            FinancialEvent.lifecycle_state == "ACTIVE",
        )
        .order_by(FinancialEvent.id.asc())
        .all()
    )


def _validate_original_has_no_reversal(db: Session, original_event_id: int) -> None:
    if _active_children(
        db,
        original_event_id=original_event_id,
        relation_type="REVERSAL_OF",
    ):
        raise CausalEventError("Original event already has an active reversal")


def _validate_no_active_refunds(db: Session, original_event_id: int) -> None:
    if _active_children(
        db,
        original_event_id=original_event_id,
        relation_type="REFUND_OF",
    ):
        raise CausalEventError(
            "Cannot reverse an event that already has active refunds"
        )


def refundable_remaining(db: Session, *, user_id: int, original_event_id: int) -> Decimal:
    original = _owned_active_event(db, user_id=user_id, event_id=original_event_id)
    if original.event_type != "EXPENSE":
        raise CausalEventError("Only EXPENSE events can be refunded")

    original_entries = _entries(db, original.id)
    if len(original_entries) != 1:
        raise CausalEventError("Refundable expense must contain exactly one entry")
    original_amount = Decimal(original_entries[0].amount)
    if original_amount >= 0:
        raise CausalEventError("Refundable expense must have a negative account entry")

    _validate_original_has_no_reversal(db, original.id)

    refunded = Decimal("0")
    for refund in _active_children(
        db,
        original_event_id=original.id,
        relation_type="REFUND_OF",
    ):
        refund_entries = _entries(db, refund.id)
        if len(refund_entries) != 1:
            raise CausalEventError("Existing refund violates one-entry invariant")
        refunded += Decimal(refund_entries[0].amount)

    remaining = -original_amount - refunded
    if remaining < 0:
        raise CausalEventError("Existing refunds exceed original expense")
    return remaining


def create_refund_event(
    db: Session,
    *,
    user_id: int,
    original_event_id: int,
    amount: Decimal,
    description: str | None,
    occurred_at: datetime.datetime | None,
    actor_type: str = "USER",
    actor_user_id: int | None = None,
    reason: str | None = None,
) -> FinancialEvent:
    amount = Decimal(amount)
    if amount <= 0:
        raise CausalEventError("Refund amount must be positive")

    original = _owned_active_event(db, user_id=user_id, event_id=original_event_id)
    if original.event_type != "EXPENSE":
        raise CausalEventError("Only EXPENSE events can be refunded")

    original_entries = _entries(db, original.id)
    if len(original_entries) != 1:
        raise CausalEventError("Refundable expense must contain exactly one entry")
    original_entry = original_entries[0]
    account = (
        db.query(FinancialAccount)
        .filter(FinancialAccount.id == original_entry.account_id)
        .one_or_none()
    )
    if account is None or account.user_id != user_id:
        raise CausalEventError("Original event account owner mismatch")

    remaining = refundable_remaining(
        db,
        user_id=user_id,
        original_event_id=original.id,
    )
    if amount > remaining:
        raise CausalEventError(
            f"Refund exceeds remaining refundable amount {format(remaining, 'f')}"
        )

    occurred = _normalize_occurred_at(occurred_at)
    event = FinancialEvent(
        user_id=user_id,
        legacy_transaction_id=None,
        event_type="REFUND",
        description=description or f"Refund of event {original.id}",
        category=original.category,
        occurred_at=occurred,
        effective_at=occurred,
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
            account_id=original_entry.account_id,
            amount=amount,
        )
    )
    db.flush()

    db.add(
        FinancialEventLink(
            from_event_id=event.id,
            to_event_id=original.id,
            relation_type="REFUND_OF",
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


def create_reversal_event(
    db: Session,
    *,
    user_id: int,
    original_event_id: int,
    description: str | None,
    occurred_at: datetime.datetime | None,
    actor_type: str = "USER",
    actor_user_id: int | None = None,
    reason: str | None = None,
) -> FinancialEvent:
    original = _owned_active_event(db, user_id=user_id, event_id=original_event_id)
    if original.event_type not in {"INCOME", "EXPENSE", "TRANSFER"}:
        raise CausalEventError(
            "Only INCOME, EXPENSE, or TRANSFER events can be reversed in V1"
        )

    _validate_original_has_no_reversal(db, original.id)
    _validate_no_active_refunds(db, original.id)

    original_entries = _entries(db, original.id)
    if not original_entries:
        raise CausalEventError("Original event has no account entries")
    for original_entry in original_entries:
        account = (
            db.query(FinancialAccount)
            .filter(FinancialAccount.id == original_entry.account_id)
            .one_or_none()
        )
        if account is None or account.user_id != user_id:
            raise CausalEventError("Original event account owner mismatch")
        if Decimal(original_entry.amount) == 0:
            raise CausalEventError("Original event contains a zero entry")

    occurred = _normalize_occurred_at(occurred_at)
    event = FinancialEvent(
        user_id=user_id,
        legacy_transaction_id=None,
        event_type="REVERSAL",
        description=description or f"Reversal of event {original.id}",
        category=original.category,
        occurred_at=occurred,
        effective_at=occurred,
        interpretation_state="USER_CONFIRMED",
        provenance="USER_MANUAL",
        confidence="USER_CONFIRMED",
        lifecycle_state="ACTIVE",
        version=1,
    )
    db.add(event)
    db.flush()

    for original_entry in original_entries:
        db.add(
            FinancialEventEntry(
                financial_event_id=event.id,
                account_id=original_entry.account_id,
                amount=-Decimal(original_entry.amount),
            )
        )
    db.flush()

    db.add(
        FinancialEventLink(
            from_event_id=event.id,
            to_event_id=original.id,
            relation_type="REVERSAL_OF",
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
