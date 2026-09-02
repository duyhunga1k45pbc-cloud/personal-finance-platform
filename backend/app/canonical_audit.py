from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal

from sqlalchemy.orm import Session

from app.canonical_service import (
    canonical_summary,
    legacy_summary,
    snapshot_financial_event,
)
from app.models import (
    FinancialAccount,
    FinancialEvent,
    FinancialEventEntry,
    FinancialEventHistory,
    Transaction,
    User,
)


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


def find_first_history_divergence(db: Session, user_id: int) -> dict | None:
    events = (
        db.query(FinancialEvent)
        .filter(FinancialEvent.user_id == user_id)
        .order_by(FinancialEvent.id.asc())
        .all()
    )

    for event in events:
        history = (
            db.query(FinancialEventHistory)
            .filter(FinancialEventHistory.financial_event_id == event.id)
            .order_by(FinancialEventHistory.event_version.asc())
            .all()
        )

        if not history:
            return {
                "financial_event_id": event.id,
                "reason": "missing_history",
            }

        expected_versions = list(range(1, event.version + 1))
        actual_versions = [row.event_version for row in history]
        if actual_versions != expected_versions:
            return {
                "financial_event_id": event.id,
                "reason": "history_version_gap",
                "expected_versions": expected_versions,
                "actual_versions": actual_versions,
            }

        if history[0].transition_type != "CREATED":
            return {
                "financial_event_id": event.id,
                "reason": "history_does_not_start_with_created",
            }

        current_snapshot = snapshot_financial_event(db, event)
        if history[-1].new_state != current_snapshot:
            return {
                "financial_event_id": event.id,
                "reason": "latest_history_snapshot_mismatch",
                "event_version": event.version,
            }

        if event.lifecycle_state == "VOIDED" and history[-1].transition_type != "VOIDED":
            return {
                "financial_event_id": event.id,
                "reason": "voided_event_missing_void_transition",
            }

    return None



def find_first_transfer_divergence(db: Session, user_id: int) -> dict | None:
    events = (
        db.query(FinancialEvent)
        .filter(
            FinancialEvent.user_id == user_id,
            FinancialEvent.event_type == "TRANSFER",
        )
        .order_by(FinancialEvent.id.asc())
        .all()
    )

    for event in events:
        entries = (
            db.query(FinancialEventEntry)
            .filter(FinancialEventEntry.financial_event_id == event.id)
            .order_by(FinancialEventEntry.id.asc())
            .all()
        )
        if len(entries) != 2:
            return {
                "financial_event_id": event.id,
                "reason": "transfer_entry_count_mismatch",
                "entry_count": len(entries),
            }

        account_ids = [entry.account_id for entry in entries]
        if len(set(account_ids)) != 2:
            return {
                "financial_event_id": event.id,
                "reason": "transfer_accounts_not_distinct",
            }

        owned_account_ids = {
            row[0]
            for row in (
                db.query(FinancialEventEntry.account_id)
                .join(
                    FinancialAccount,
                    FinancialAccount.id == FinancialEventEntry.account_id,
                )
                .filter(
                    FinancialEventEntry.financial_event_id == event.id,
                    FinancialAccount.user_id == user_id,
                )
                .all()
            )
        }
        if owned_account_ids != set(account_ids):
            return {
                "financial_event_id": event.id,
                "reason": "transfer_account_owner_mismatch",
            }

        amounts = [Decimal(entry.amount) for entry in entries]
        if sum(amounts, Decimal("0")) != Decimal("0"):
            return {
                "financial_event_id": event.id,
                "reason": "transfer_not_zero_sum",
                "amounts": [str(amount) for amount in amounts],
            }
        if len([amount for amount in amounts if amount < 0]) != 1 or len(
            [amount for amount in amounts if amount > 0]
        ) != 1:
            return {
                "financial_event_id": event.id,
                "reason": "transfer_direction_invalid",
                "amounts": [str(amount) for amount in amounts],
            }

    return None


def audit_user(db: Session, user_id: int) -> dict:
    legacy = legacy_summary(db, user_id)
    canonical = canonical_summary(db, user_id)
    first_divergence = find_first_divergence(db, user_id)
    history_divergence = find_first_history_divergence(db, user_id)
    transfer_divergence = find_first_transfer_divergence(db, user_id)

    summary_match = legacy == canonical
    return {
        "user_id": user_id,
        "ok": (
            summary_match
            and first_divergence is None
            and history_divergence is None
            and transfer_divergence is None
        ),
        "legacy": {key: str(value) for key, value in asdict(legacy).items()},
        "canonical": {
            key: str(value) for key, value in asdict(canonical).items()
        },
        "summary_match": summary_match,
        "first_divergence": first_divergence,
        "history_divergence": history_divergence,
        "transfer_divergence": transfer_divergence,
    }


def audit_all_users(db: Session) -> list[dict]:
    user_ids = [row[0] for row in db.query(User.id).order_by(User.id.asc()).all()]
    return [audit_user(db, user_id) for user_id in user_ids]
