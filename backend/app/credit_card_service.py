from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import FinancialAccount, FinancialEvent, FinancialEventEntry


def validate_transaction_account_semantics(
    account: FinancialAccount,
    transaction_type: str,
) -> None:
    """Enforce V1 semantics for legacy income/expense commands.

    A purchase on a credit card is an EXPENSE whose account movement is
    negative: the user's liability increases. Paying the card is not another
    expense; it is represented by a TRANSFER into the credit-card account.

    Direct INCOME on a credit card is intentionally rejected. A merchant
    refund will be represented by REFUND semantics in a later slice instead of
    being disguised as income.
    """

    if account.account_type == "CREDIT_CARD" and transaction_type == "income":
        raise ValueError(
            "Income cannot be recorded directly on a credit card account; "
            "card repayments must use transfers and refunds use refund semantics"
        )


def canonical_account_position(
    db: Session,
    *,
    user_id: int,
    account_id: int,
) -> Decimal:
    """Return the signed canonical position accumulated for one account.

    This is a state delta from canonical events, not an externally observed
    provider balance. For CREDIT_CARD accounts, a negative value means an
    outstanding liability and a positive movement reduces that liability.
    """

    account = (
        db.query(FinancialAccount)
        .filter(
            FinancialAccount.id == account_id,
            FinancialAccount.user_id == user_id,
        )
        .one_or_none()
    )
    if account is None:
        raise ValueError("Account not found for user")

    value = (
        db.query(func.coalesce(func.sum(FinancialEventEntry.amount), 0))
        .join(
            FinancialEvent,
            FinancialEvent.id == FinancialEventEntry.financial_event_id,
        )
        .filter(
            FinancialEventEntry.account_id == account_id,
            FinancialEvent.user_id == user_id,
            FinancialEvent.lifecycle_state == "ACTIVE",
        )
        .scalar()
    )
    return Decimal(value or 0)


def credit_card_liability(
    db: Session,
    *,
    user_id: int,
    account_id: int,
) -> Decimal:
    account = (
        db.query(FinancialAccount)
        .filter(
            FinancialAccount.id == account_id,
            FinancialAccount.user_id == user_id,
        )
        .one_or_none()
    )
    if account is None:
        raise ValueError("Account not found for user")
    if account.account_type != "CREDIT_CARD":
        raise ValueError("Account is not a credit card")

    position = canonical_account_position(
        db,
        user_id=user_id,
        account_id=account_id,
    )
    return max(-position, Decimal("0"))
