from __future__ import annotations

import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.canonical_service import (
    append_financial_event_history,
    snapshot_financial_event,
)
from app.models import FinancialAccount, FinancialEvent, FinancialEventEntry


def _normalize_occurred_at(value: datetime.datetime | None) -> datetime.datetime:
    if value is None:
        return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    if value.tzinfo is None:
        return value
    return value.astimezone(datetime.timezone.utc).replace(tzinfo=None)


def validate_transfer_accounts(
    *,
    user_id: int,
    from_account: FinancialAccount,
    to_account: FinancialAccount,
) -> None:
    if from_account.user_id != user_id or to_account.user_id != user_id:
        raise ValueError("Transfer accounts must belong to the same user")
    if from_account.id == to_account.id:
        raise ValueError("Transfer source and destination accounts must be different")
    if from_account.currency != "VND" or to_account.currency != "VND":
        raise ValueError("V1 transfers require VND accounts")


def create_transfer_event(
    db: Session,
    *,
    user_id: int,
    from_account: FinancialAccount,
    to_account: FinancialAccount,
    amount: Decimal,
    description: str | None,
    occurred_at: datetime.datetime | None,
    actor_type: str = "USER",
    actor_user_id: int | None = None,
    reason: str | None = None,
) -> FinancialEvent:
    amount = Decimal(amount)
    if amount <= 0:
        raise ValueError("Transfer amount must be positive")

    validate_transfer_accounts(
        user_id=user_id,
        from_account=from_account,
        to_account=to_account,
    )

    event_time = _normalize_occurred_at(occurred_at)
    event = FinancialEvent(
        user_id=user_id,
        legacy_transaction_id=None,
        event_type="TRANSFER",
        description=description,
        category=None,
        occurred_at=event_time,
        effective_at=event_time,
        interpretation_state="USER_CONFIRMED",
        provenance="USER_MANUAL",
        confidence="USER_CONFIRMED",
        lifecycle_state="ACTIVE",
        version=1,
    )
    db.add(event)
    db.flush()

    db.add_all(
        [
            FinancialEventEntry(
                financial_event_id=event.id,
                account_id=from_account.id,
                amount=-amount,
            ),
            FinancialEventEntry(
                financial_event_id=event.id,
                account_id=to_account.id,
                amount=amount,
            ),
        ]
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
