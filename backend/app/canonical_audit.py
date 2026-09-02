from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal

from sqlalchemy.orm import Session

from app.canonical_service import canonical_summary, legacy_summary
from app.models import FinancialEvent, FinancialEventEntry, Transaction, User


def _expected_signed_amount(transaction: Transaction) -> Decimal:
    amount = Decimal(transaction.amount)
    if transaction.type == "income":
        return amount
    if transaction.type == "expense":
        return -amount
    raise ValueError(f"Unsupported transaction type: {transaction.type}")


def find_first_divergence(db: Session, user_id: int) -> dict | None:
    transactions = (
        db.query(Transaction)
        .filter(Transaction.user_id == user_id)
        .order_by(Transaction.id.asc())
        .all()
    )

    for transaction in transactions:
        event = (
            db.query(FinancialEvent)
            .filter(FinancialEvent.legacy_transaction_id == transaction.id)
            .one_or_none()
        )
        if event is None:
            return {
                "legacy_transaction_id": transaction.id,
                "reason": "missing_canonical_event",
            }

        if event.user_id != transaction.user_id:
            return {
                "legacy_transaction_id": transaction.id,
                "reason": "owner_mismatch",
                "legacy_user_id": transaction.user_id,
                "canonical_user_id": event.user_id,
            }

        expected_type = transaction.type.upper()
        if event.event_type != expected_type:
            return {
                "legacy_transaction_id": transaction.id,
                "reason": "event_type_mismatch",
                "legacy": expected_type,
                "canonical": event.event_type,
            }

        if event.description != transaction.description:
            return {
                "legacy_transaction_id": transaction.id,
                "reason": "description_mismatch",
            }

        if event.category != transaction.category:
            return {
                "legacy_transaction_id": transaction.id,
                "reason": "category_mismatch",
            }

        if event.occurred_at != transaction.date:
            return {
                "legacy_transaction_id": transaction.id,
                "reason": "occurred_at_mismatch",
                "legacy": str(transaction.date),
                "canonical": str(event.occurred_at),
            }

        entries = (
            db.query(FinancialEventEntry)
            .filter(FinancialEventEntry.financial_event_id == event.id)
            .all()
        )
        if len(entries) != 1:
            return {
                "legacy_transaction_id": transaction.id,
                "reason": "entry_count_mismatch",
                "entry_count": len(entries),
            }

        entry = entries[0]
        if entry.account_id != transaction.account_id:
            return {
                "legacy_transaction_id": transaction.id,
                "reason": "account_mismatch",
                "legacy_account_id": transaction.account_id,
                "canonical_account_id": entry.account_id,
            }

        expected_amount = _expected_signed_amount(transaction)
        if Decimal(entry.amount) != expected_amount:
            return {
                "legacy_transaction_id": transaction.id,
                "reason": "amount_mismatch",
                "legacy_signed_amount": str(expected_amount),
                "canonical_amount": str(entry.amount),
            }

    return None


def audit_user(db: Session, user_id: int) -> dict:
    legacy = legacy_summary(db, user_id)
    canonical = canonical_summary(db, user_id)
    first_divergence = find_first_divergence(db, user_id)

    summary_match = legacy == canonical
    return {
        "user_id": user_id,
        "ok": summary_match and first_divergence is None,
        "legacy": {key: str(value) for key, value in asdict(legacy).items()},
        "canonical": {
            key: str(value) for key, value in asdict(canonical).items()
        },
        "summary_match": summary_match,
        "first_divergence": first_divergence,
    }


def audit_all_users(db: Session) -> list[dict]:
    user_ids = [row[0] for row in db.query(User.id).order_by(User.id.asc()).all()]
    return [audit_user(db, user_id) for user_id in user_ids]
