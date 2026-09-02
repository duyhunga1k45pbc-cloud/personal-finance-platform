from __future__ import annotations

from dataclasses import asdict
import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.canonical_service import (
    canonical_legacy_summary,
    canonical_summary,
    legacy_summary,
    snapshot_financial_event,
)
from app.models import (
    FinancialAccount,
    FinancialEvent,
    FinancialEventEntry,
    FinancialEventHistory,
    FinancialEventLink,
    ReconciliationCase,
    ReconciliationHistory,
    ProviderConnection,
    ExternalTransaction,
    ExternalTransactionEvidence,
    ProviderNormalizedCandidate,
    ProviderTransactionInterpretation,
    ProviderInterpretationHistory,
    ProviderTransactionLifecycle,
    ProviderTransactionLifecycleHistory,
    ProviderSyncCheckpoint,
    ProviderSyncPage,
    ProviderSyncPageEvidence,
    FinancialProjectionState,
    FinancialAccountBalanceProjection,
    Transaction,
    User,
)
from app.provider_interpretation_service import snapshot_provider_interpretation
from app.provider_lifecycle_service import snapshot_provider_lifecycle
from app.provider_service import hash_raw_payload
from app.provider_sync_service import ProviderSyncObservation, sync_page_hash
from app.projection_service import compute_projection
from app.reconciliation_service import snapshot_reconciliation


def _utc_datetime(value: datetime.datetime) -> datetime.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.timezone.utc)
    return value.astimezone(datetime.timezone.utc)


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



def find_first_credit_card_divergence(db: Session, user_id: int) -> dict | None:
    """Validate V1 credit-card accounting semantics.

    A credit-card purchase is an EXPENSE with a negative card entry (liability
    increases). Repayments are TRANSFER events, so they never count as a second
    expense. Direct INCOME on a credit-card account is not valid V1 semantics.
    """

    rows = (
        db.query(FinancialEvent, FinancialEventEntry, FinancialAccount)
        .join(
            FinancialEventEntry,
            FinancialEventEntry.financial_event_id == FinancialEvent.id,
        )
        .join(
            FinancialAccount,
            FinancialAccount.id == FinancialEventEntry.account_id,
        )
        .filter(
            FinancialEvent.user_id == user_id,
            FinancialEvent.lifecycle_state == "ACTIVE",
            FinancialAccount.user_id == user_id,
            FinancialAccount.account_type == "CREDIT_CARD",
        )
        .order_by(FinancialEvent.id.asc(), FinancialEventEntry.id.asc())
        .all()
    )

    for event, entry, account in rows:
        amount = Decimal(entry.amount)
        if event.event_type == "INCOME":
            return {
                "financial_event_id": event.id,
                "account_id": account.id,
                "reason": "income_on_credit_card",
            }

        if event.event_type == "EXPENSE":
            entries = (
                db.query(FinancialEventEntry)
                .filter(FinancialEventEntry.financial_event_id == event.id)
                .all()
            )
            if len(entries) != 1:
                return {
                    "financial_event_id": event.id,
                    "account_id": account.id,
                    "reason": "credit_card_expense_entry_count_mismatch",
                    "entry_count": len(entries),
                }
            if amount >= 0:
                return {
                    "financial_event_id": event.id,
                    "account_id": account.id,
                    "reason": "credit_card_purchase_not_liability_increase",
                    "amount": str(amount),
                }

    return None

def find_first_causal_divergence(db: Session, user_id: int) -> dict | None:
    events = (
        db.query(FinancialEvent)
        .filter(
            FinancialEvent.user_id == user_id,
            FinancialEvent.event_type.in_(["REFUND", "REVERSAL"]),
        )
        .order_by(FinancialEvent.id.asc())
        .all()
    )

    for child in events:
        links = (
            db.query(FinancialEventLink)
            .filter(FinancialEventLink.from_event_id == child.id)
            .all()
        )
        if len(links) != 1:
            return {
                "financial_event_id": child.id,
                "reason": "causal_link_count_mismatch",
                "link_count": len(links),
            }
        link = links[0]
        expected_relation = "REFUND_OF" if child.event_type == "REFUND" else "REVERSAL_OF"
        if link.relation_type != expected_relation:
            return {
                "financial_event_id": child.id,
                "reason": "causal_relation_type_mismatch",
                "expected": expected_relation,
                "actual": link.relation_type,
            }
        original = (
            db.query(FinancialEvent)
            .filter(FinancialEvent.id == link.to_event_id)
            .one_or_none()
        )
        if original is None:
            return {
                "financial_event_id": child.id,
                "reason": "causal_original_missing",
            }
        if original.user_id != user_id or child.user_id != original.user_id:
            return {
                "financial_event_id": child.id,
                "reason": "causal_owner_mismatch",
            }
        if child.legacy_transaction_id is not None:
            return {
                "financial_event_id": child.id,
                "reason": "causal_child_must_be_canonical_only",
            }

        child_entries = (
            db.query(FinancialEventEntry)
            .filter(FinancialEventEntry.financial_event_id == child.id)
            .order_by(FinancialEventEntry.account_id.asc())
            .all()
        )
        original_entries = (
            db.query(FinancialEventEntry)
            .filter(FinancialEventEntry.financial_event_id == original.id)
            .order_by(FinancialEventEntry.account_id.asc())
            .all()
        )

        if child.event_type == "REFUND":
            if original.event_type != "EXPENSE":
                return {
                    "financial_event_id": child.id,
                    "reason": "refund_original_not_expense",
                }
            if len(child_entries) != 1 or len(original_entries) != 1:
                return {
                    "financial_event_id": child.id,
                    "reason": "refund_entry_count_mismatch",
                }
            if child_entries[0].account_id != original_entries[0].account_id:
                return {
                    "financial_event_id": child.id,
                    "reason": "refund_account_mismatch",
                }
            if Decimal(child_entries[0].amount) <= 0 or Decimal(original_entries[0].amount) >= 0:
                return {
                    "financial_event_id": child.id,
                    "reason": "refund_direction_invalid",
                }

        if child.event_type == "REVERSAL":
            if original.event_type not in {"INCOME", "EXPENSE", "TRANSFER"}:
                return {
                    "financial_event_id": child.id,
                    "reason": "reversal_original_type_invalid",
                }
            child_map = {row.account_id: Decimal(row.amount) for row in child_entries}
            original_map = {row.account_id: Decimal(row.amount) for row in original_entries}
            if set(child_map) != set(original_map):
                return {
                    "financial_event_id": child.id,
                    "reason": "reversal_accounts_mismatch",
                }
            for account_id, amount in original_map.items():
                if child_map[account_id] != -amount:
                    return {
                        "financial_event_id": child.id,
                        "reason": "reversal_amount_not_exact_inverse",
                        "account_id": account_id,
                    }

    originals = (
        db.query(FinancialEvent)
        .filter(FinancialEvent.user_id == user_id)
        .order_by(FinancialEvent.id.asc())
        .all()
    )
    for original in originals:
        linked = (
            db.query(FinancialEventLink, FinancialEvent)
            .join(FinancialEvent, FinancialEvent.id == FinancialEventLink.from_event_id)
            .filter(
                FinancialEventLink.to_event_id == original.id,
                FinancialEvent.lifecycle_state == "ACTIVE",
            )
            .all()
        )
        refunds = [pair for pair in linked if pair[0].relation_type == "REFUND_OF"]
        reversals = [pair for pair in linked if pair[0].relation_type == "REVERSAL_OF"]
        if len(reversals) > 1:
            return {
                "financial_event_id": original.id,
                "reason": "multiple_active_reversals",
            }
        if refunds and reversals:
            return {
                "financial_event_id": original.id,
                "reason": "refund_and_reversal_conflict",
            }
        if refunds:
            original_entries = (
                db.query(FinancialEventEntry)
                .filter(FinancialEventEntry.financial_event_id == original.id)
                .all()
            )
            if len(original_entries) != 1:
                return {
                    "financial_event_id": original.id,
                    "reason": "refunded_original_entry_count_mismatch",
                }
            total_refund = Decimal("0")
            for _, refund_event in refunds:
                refund_entries = (
                    db.query(FinancialEventEntry)
                    .filter(FinancialEventEntry.financial_event_id == refund_event.id)
                    .all()
                )
                if len(refund_entries) != 1:
                    return {
                        "financial_event_id": refund_event.id,
                        "reason": "refund_entry_count_mismatch",
                    }
                total_refund += Decimal(refund_entries[0].amount)
            if total_refund > -Decimal(original_entries[0].amount):
                return {
                    "financial_event_id": original.id,
                    "reason": "refunds_exceed_original_expense",
                    "refund_total": str(total_refund),
                }

    return None


def find_first_reconciliation_divergence(db: Session, user_id: int) -> dict | None:
    cases = (
        db.query(ReconciliationCase)
        .filter(ReconciliationCase.user_id == user_id)
        .order_by(ReconciliationCase.id.asc())
        .all()
    )

    for case in cases:
        account = (
            db.query(FinancialAccount)
            .filter(FinancialAccount.id == case.account_id)
            .one_or_none()
        )
        if account is None or account.user_id != user_id:
            return {
                "reconciliation_id": case.id,
                "reason": "reconciliation_account_owner_mismatch",
            }
        if account.account_type != "CASH":
            return {
                "reconciliation_id": case.id,
                "reason": "reconciliation_non_cash_account",
                "account_type": account.account_type,
            }

        expected_difference = Decimal(case.observed_balance) - Decimal(case.expected_balance)
        if Decimal(case.difference) != expected_difference:
            return {
                "reconciliation_id": case.id,
                "reason": "reconciliation_difference_mismatch",
                "expected": str(expected_difference),
                "actual": str(case.difference),
            }

        history = (
            db.query(ReconciliationHistory)
            .filter(ReconciliationHistory.reconciliation_id == case.id)
            .order_by(ReconciliationHistory.reconciliation_version.asc())
            .all()
        )
        if not history:
            return {
                "reconciliation_id": case.id,
                "reason": "reconciliation_missing_history",
            }
        expected_versions = list(range(1, case.version + 1))
        actual_versions = [row.reconciliation_version for row in history]
        if actual_versions != expected_versions:
            return {
                "reconciliation_id": case.id,
                "reason": "reconciliation_history_version_gap",
                "expected_versions": expected_versions,
                "actual_versions": actual_versions,
            }
        if history[0].transition_type != "DETECTED":
            return {
                "reconciliation_id": case.id,
                "reason": "reconciliation_history_does_not_start_detected",
            }
        if history[-1].new_state != snapshot_reconciliation(case):
            return {
                "reconciliation_id": case.id,
                "reason": "reconciliation_latest_history_snapshot_mismatch",
            }

        if case.status == "RECONCILED":
            if Decimal(case.difference) != 0:
                return {
                    "reconciliation_id": case.id,
                    "reason": "reconciled_case_has_nonzero_difference",
                }
            if case.version != 1 or history[-1].transition_type != "DETECTED":
                return {
                    "reconciliation_id": case.id,
                    "reason": "reconciled_case_has_resolution_transition",
                }
        elif case.status == "MISMATCH":
            if Decimal(case.difference) == 0:
                return {
                    "reconciliation_id": case.id,
                    "reason": "mismatch_case_has_zero_difference",
                }
            if case.resolution_type is not None or case.adjustment_event_id is not None:
                return {
                    "reconciliation_id": case.id,
                    "reason": "unresolved_mismatch_has_resolution",
                }
        elif case.status == "RESOLVED":
            if case.resolved_balance is None or Decimal(case.resolved_balance) != Decimal(case.observed_balance):
                return {
                    "reconciliation_id": case.id,
                    "reason": "resolved_balance_does_not_match_observation",
                }
            if case.resolution_type == "REAL_EVENT":
                if case.adjustment_event_id is not None:
                    return {
                        "reconciliation_id": case.id,
                        "reason": "real_event_resolution_has_adjustment",
                    }
                if history[-1].transition_type != "RESOLVED_REAL_EVENT":
                    return {
                        "reconciliation_id": case.id,
                        "reason": "real_event_resolution_history_mismatch",
                    }
            elif case.resolution_type == "ADJUSTMENT":
                if case.adjustment_event_id is None:
                    return {
                        "reconciliation_id": case.id,
                        "reason": "adjustment_resolution_missing_event",
                    }
                if history[-1].transition_type != "RESOLVED_ADJUSTMENT":
                    return {
                        "reconciliation_id": case.id,
                        "reason": "adjustment_resolution_history_mismatch",
                    }
                event = (
                    db.query(FinancialEvent)
                    .filter(FinancialEvent.id == case.adjustment_event_id)
                    .one_or_none()
                )
                if event is None or event.user_id != user_id:
                    return {
                        "reconciliation_id": case.id,
                        "reason": "adjustment_event_owner_mismatch",
                    }
                if event.event_type != "ADJUSTMENT" or event.lifecycle_state != "ACTIVE":
                    return {
                        "reconciliation_id": case.id,
                        "reason": "adjustment_event_state_invalid",
                    }
                if event.legacy_transaction_id is not None:
                    return {
                        "reconciliation_id": case.id,
                        "reason": "adjustment_event_must_be_canonical_only",
                    }
                entries = (
                    db.query(FinancialEventEntry)
                    .filter(FinancialEventEntry.financial_event_id == event.id)
                    .all()
                )
                if len(entries) != 1:
                    return {
                        "reconciliation_id": case.id,
                        "reason": "adjustment_entry_count_mismatch",
                    }
                entry = entries[0]
                if entry.account_id != case.account_id:
                    return {
                        "reconciliation_id": case.id,
                        "reason": "adjustment_account_mismatch",
                    }
                if Decimal(entry.amount) != Decimal(case.difference):
                    return {
                        "reconciliation_id": case.id,
                        "reason": "adjustment_amount_does_not_close_original_mismatch",
                        "expected": str(case.difference),
                        "actual": str(entry.amount),
                    }
            else:
                return {
                    "reconciliation_id": case.id,
                    "reason": "resolved_case_resolution_type_invalid",
                }
        else:
            return {
                "reconciliation_id": case.id,
                "reason": "reconciliation_status_unsupported",
                "status": case.status,
            }

    adjustments = (
        db.query(FinancialEvent)
        .filter(
            FinancialEvent.user_id == user_id,
            FinancialEvent.event_type == "ADJUSTMENT",
        )
        .order_by(FinancialEvent.id.asc())
        .all()
    )
    for event in adjustments:
        references = (
            db.query(ReconciliationCase)
            .filter(
                ReconciliationCase.user_id == user_id,
                ReconciliationCase.adjustment_event_id == event.id,
                ReconciliationCase.resolution_type == "ADJUSTMENT",
            )
            .count()
        )
        if references != 1:
            return {
                "financial_event_id": event.id,
                "reason": "orphan_or_multiply_referenced_adjustment",
                "reference_count": references,
            }

    return None



def find_first_provider_evidence_divergence(db: Session, user_id: int) -> dict | None:
    transactions = (
        db.query(ExternalTransaction)
        .filter(ExternalTransaction.user_id == user_id)
        .order_by(ExternalTransaction.id.asc())
        .all()
    )

    for transaction in transactions:
        connection = (
            db.query(ProviderConnection)
            .filter(ProviderConnection.id == transaction.provider_connection_id)
            .one_or_none()
        )
        if connection is None:
            return {
                "external_transaction_record_id": transaction.id,
                "reason": "provider_connection_missing",
            }
        if connection.user_id != user_id:
            return {
                "external_transaction_record_id": transaction.id,
                "reason": "provider_connection_owner_mismatch",
                "transaction_user_id": transaction.user_id,
                "connection_user_id": connection.user_id,
            }
        if not transaction.external_transaction_id.strip():
            return {
                "external_transaction_record_id": transaction.id,
                "reason": "external_transaction_identity_blank",
            }

        evidence_rows = (
            db.query(ExternalTransactionEvidence)
            .filter(
                ExternalTransactionEvidence.external_transaction_record_id == transaction.id
            )
            .order_by(ExternalTransactionEvidence.id.asc())
            .all()
        )
        if not evidence_rows:
            return {
                "external_transaction_record_id": transaction.id,
                "reason": "external_transaction_missing_raw_evidence",
            }

        for evidence in evidence_rows:
            if evidence.user_id != user_id:
                return {
                    "external_transaction_record_id": transaction.id,
                    "evidence_id": evidence.id,
                    "reason": "external_evidence_owner_mismatch",
                }
            if evidence.provider_connection_id != transaction.provider_connection_id:
                return {
                    "external_transaction_record_id": transaction.id,
                    "evidence_id": evidence.id,
                    "reason": "external_evidence_connection_mismatch",
                }
            expected_hash = hash_raw_payload(evidence.raw_payload)
            if evidence.payload_sha256 != expected_hash:
                return {
                    "external_transaction_record_id": transaction.id,
                    "evidence_id": evidence.id,
                    "reason": "external_evidence_payload_hash_mismatch",
                    "expected": expected_hash,
                    "actual": evidence.payload_sha256,
                }

    return None



def find_first_provider_interpretation_divergence(db: Session, user_id: int) -> dict | None:
    candidates = (
        db.query(ProviderNormalizedCandidate)
        .filter(ProviderNormalizedCandidate.user_id == user_id)
        .order_by(ProviderNormalizedCandidate.id.asc())
        .all()
    )
    for candidate in candidates:
        transaction = db.query(ExternalTransaction).filter(ExternalTransaction.id == candidate.external_transaction_record_id).one_or_none()
        evidence = db.query(ExternalTransactionEvidence).filter(ExternalTransactionEvidence.id == candidate.source_evidence_id).one_or_none()
        if transaction is None or evidence is None:
            return {"normalized_candidate_id": candidate.id, "reason": "normalized_source_missing"}
        if transaction.user_id != user_id or evidence.user_id != user_id:
            return {"normalized_candidate_id": candidate.id, "reason": "normalized_source_owner_mismatch"}
        if candidate.provider_connection_id != transaction.provider_connection_id or candidate.provider_connection_id != evidence.provider_connection_id:
            return {"normalized_candidate_id": candidate.id, "reason": "normalized_connection_mismatch"}
        if evidence.external_transaction_record_id != transaction.id:
            return {"normalized_candidate_id": candidate.id, "reason": "normalized_evidence_transaction_mismatch"}
        if Decimal(candidate.amount) <= 0:
            return {"normalized_candidate_id": candidate.id, "reason": "normalized_amount_nonpositive"}

    interpretations = (
        db.query(ProviderTransactionInterpretation)
        .filter(ProviderTransactionInterpretation.user_id == user_id)
        .order_by(ProviderTransactionInterpretation.id.asc())
        .all()
    )
    for interp in interpretations:
        transaction = db.query(ExternalTransaction).filter(ExternalTransaction.id == interp.external_transaction_record_id).one_or_none()
        candidate = db.query(ProviderNormalizedCandidate).filter(ProviderNormalizedCandidate.id == interp.normalized_candidate_id).one_or_none()
        if transaction is None or candidate is None:
            return {"provider_interpretation_id": interp.id, "reason": "interpretation_source_missing"}
        if transaction.user_id != user_id or candidate.user_id != user_id:
            return {"provider_interpretation_id": interp.id, "reason": "interpretation_owner_mismatch"}
        if candidate.external_transaction_record_id != transaction.id:
            return {"provider_interpretation_id": interp.id, "reason": "interpretation_candidate_transaction_mismatch"}

        history = (
            db.query(ProviderInterpretationHistory)
            .filter(ProviderInterpretationHistory.interpretation_id == interp.id)
            .order_by(ProviderInterpretationHistory.interpretation_version.asc())
            .all()
        )
        expected_versions = list(range(1, interp.version + 1))
        actual_versions = [row.interpretation_version for row in history]
        if actual_versions != expected_versions:
            return {
                "provider_interpretation_id": interp.id,
                "reason": "interpretation_history_version_gap",
                "expected_versions": expected_versions,
                "actual_versions": actual_versions,
            }
        if not history or history[0].transition_type != "NORMALIZED":
            return {"provider_interpretation_id": interp.id, "reason": "interpretation_history_does_not_start_normalized"}
        if history[-1].new_state != snapshot_provider_interpretation(interp):
            return {"provider_interpretation_id": interp.id, "reason": "interpretation_history_snapshot_mismatch"}

        if interp.state == "UNCLASSIFIED":
            if interp.event_type is not None or interp.account_id is not None or interp.canonical_event_id is not None:
                return {"provider_interpretation_id": interp.id, "reason": "unclassified_state_has_semantics"}
        elif interp.state == "CLASSIFIED":
            if interp.event_type is None or interp.confidence != "INFERRED" or interp.account_id is not None or interp.canonical_event_id is not None:
                return {"provider_interpretation_id": interp.id, "reason": "classified_state_shape_invalid"}
        elif interp.state == "USER_CONFIRMED":
            if interp.event_type is None or interp.account_id is None or interp.confidence != "USER_CONFIRMED":
                return {"provider_interpretation_id": interp.id, "reason": "confirmed_state_shape_invalid"}
        else:
            return {"provider_interpretation_id": interp.id, "reason": "interpretation_state_invalid"}

        if interp.canonical_event_id is not None:
            event = db.query(FinancialEvent).filter(FinancialEvent.id == interp.canonical_event_id).one_or_none()
            if event is None or event.user_id != user_id:
                return {"provider_interpretation_id": interp.id, "reason": "provider_canonical_event_missing_or_owner_mismatch"}
            if event.provenance != "PROVIDER" or event.interpretation_state != "USER_CONFIRMED" or event.event_type != interp.event_type:
                return {"provider_interpretation_id": interp.id, "reason": "provider_canonical_event_semantics_mismatch"}
            entries = db.query(FinancialEventEntry).filter(FinancialEventEntry.financial_event_id == event.id).all()
            if len(entries) != 1 or entries[0].account_id != interp.account_id:
                return {"provider_interpretation_id": interp.id, "reason": "provider_canonical_entry_shape_mismatch"}
            expected_amount = Decimal(candidate.amount) if interp.event_type == "INCOME" else -Decimal(candidate.amount)
            if Decimal(entries[0].amount) != expected_amount:
                return {"provider_interpretation_id": interp.id, "reason": "provider_canonical_amount_mismatch"}
            if candidate.normalized_status != "POSTED" or candidate.currency != "VND":
                return {"provider_interpretation_id": interp.id, "reason": "provider_canonical_materialized_from_ineligible_candidate"}

        if (
            interp.state == "USER_CONFIRMED"
            and interp.event_type in {"INCOME", "EXPENSE"}
            and candidate.normalized_status == "POSTED"
            and candidate.currency == "VND"
            and interp.canonical_event_id is None
        ):
            return {"provider_interpretation_id": interp.id, "reason": "eligible_confirmed_provider_event_not_materialized"}

    return None



def find_first_provider_lifecycle_divergence(db: Session, user_id: int) -> dict | None:
    lifecycles = (
        db.query(ProviderTransactionLifecycle)
        .filter(ProviderTransactionLifecycle.user_id == user_id)
        .order_by(ProviderTransactionLifecycle.id.asc())
        .all()
    )

    for lifecycle in lifecycles:
        transaction = (
            db.query(ExternalTransaction)
            .filter(ExternalTransaction.id == lifecycle.external_transaction_record_id)
            .one_or_none()
        )
        candidate = (
            db.query(ProviderNormalizedCandidate)
            .filter(ProviderNormalizedCandidate.id == lifecycle.current_candidate_id)
            .one_or_none()
        )
        if transaction is None or candidate is None:
            return {
                "provider_lifecycle_id": lifecycle.id,
                "reason": "provider_lifecycle_source_missing",
            }
        if transaction.user_id != user_id or candidate.user_id != user_id:
            return {
                "provider_lifecycle_id": lifecycle.id,
                "reason": "provider_lifecycle_owner_mismatch",
            }
        if candidate.external_transaction_record_id != transaction.id:
            return {
                "provider_lifecycle_id": lifecycle.id,
                "reason": "provider_lifecycle_candidate_transaction_mismatch",
            }
        if candidate.normalized_status != lifecycle.current_status:
            return {
                "provider_lifecycle_id": lifecycle.id,
                "reason": "provider_lifecycle_status_candidate_mismatch",
                "lifecycle_status": lifecycle.current_status,
                "candidate_status": candidate.normalized_status,
            }
        evidence = (
            db.query(ExternalTransactionEvidence)
            .filter(ExternalTransactionEvidence.id == candidate.source_evidence_id)
            .one_or_none()
        )
        if evidence is None:
            return {
                "provider_lifecycle_id": lifecycle.id,
                "reason": "provider_lifecycle_evidence_missing",
            }
        if _utc_datetime(evidence.observed_at) != _utc_datetime(lifecycle.current_observed_at):
            return {
                "provider_lifecycle_id": lifecycle.id,
                "reason": "provider_lifecycle_observed_at_mismatch",
            }

        # The current lifecycle must not lag behind a newer non-UNKNOWN immutable
        # observation. Older observations normalized late are allowed and ignored.
        candidate_rows = (
            db.query(ProviderNormalizedCandidate, ExternalTransactionEvidence)
            .join(
                ExternalTransactionEvidence,
                ExternalTransactionEvidence.id == ProviderNormalizedCandidate.source_evidence_id,
            )
            .filter(
                ProviderNormalizedCandidate.external_transaction_record_id == transaction.id,
                ProviderNormalizedCandidate.normalized_status != "UNKNOWN",
            )
            .all()
        )
        allowed_from_current = {
            "PENDING": {"PENDING", "POSTED", "REVERSED"},
            "POSTED": {"POSTED", "REVERSED"},
            "REVERSED": {"REVERSED"},
        }[lifecycle.current_status]
        for other_candidate, other_evidence in candidate_rows:
            if (
                _utc_datetime(other_evidence.observed_at) > _utc_datetime(lifecycle.current_observed_at)
                and other_candidate.normalized_status in allowed_from_current
            ):
                return {
                    "provider_lifecycle_id": lifecycle.id,
                    "normalized_candidate_id": other_candidate.id,
                    "reason": "provider_lifecycle_lags_newer_observation",
                }

        history = (
            db.query(ProviderTransactionLifecycleHistory)
            .filter(ProviderTransactionLifecycleHistory.lifecycle_id == lifecycle.id)
            .order_by(ProviderTransactionLifecycleHistory.lifecycle_version.asc())
            .all()
        )
        expected_versions = list(range(1, lifecycle.version + 1))
        actual_versions = [row.lifecycle_version for row in history]
        if actual_versions != expected_versions:
            return {
                "provider_lifecycle_id": lifecycle.id,
                "reason": "provider_lifecycle_history_version_gap",
                "expected_versions": expected_versions,
                "actual_versions": actual_versions,
            }
        if not history or history[0].transition_type != "INITIALIZED":
            return {
                "provider_lifecycle_id": lifecycle.id,
                "reason": "provider_lifecycle_history_does_not_start_initialized",
            }
        for index, row in enumerate(history):
            source_candidate = (
                db.query(ProviderNormalizedCandidate)
                .filter(ProviderNormalizedCandidate.id == row.source_candidate_id)
                .one_or_none()
            )
            if source_candidate is None or source_candidate.user_id != user_id:
                return {
                    "provider_lifecycle_id": lifecycle.id,
                    "reason": "provider_lifecycle_history_source_candidate_invalid",
                }
            if source_candidate.external_transaction_record_id != transaction.id:
                return {
                    "provider_lifecycle_id": lifecycle.id,
                    "reason": "provider_lifecycle_history_transaction_mismatch",
                }
            if index == 0:
                if row.previous_state is not None:
                    return {
                        "provider_lifecycle_id": lifecycle.id,
                        "reason": "provider_lifecycle_initial_history_has_previous_state",
                    }
            elif row.previous_state != history[index - 1].new_state:
                return {
                    "provider_lifecycle_id": lifecycle.id,
                    "reason": "provider_lifecycle_history_chain_break",
                    "lifecycle_version": row.lifecycle_version,
                }
        if history[-1].new_state != snapshot_provider_lifecycle(lifecycle):
            return {
                "provider_lifecycle_id": lifecycle.id,
                "reason": "provider_lifecycle_latest_history_snapshot_mismatch",
            }

        if lifecycle.current_status == "PENDING":
            if any(
                value is not None
                for value in (
                    lifecycle.posted_candidate_id,
                    lifecycle.reversed_candidate_id,
                    lifecycle.canonical_event_id,
                    lifecycle.reversal_event_id,
                )
            ):
                return {
                    "provider_lifecycle_id": lifecycle.id,
                    "reason": "pending_provider_lifecycle_has_downstream_state",
                }
        elif lifecycle.current_status == "POSTED":
            if lifecycle.posted_candidate_id is None or lifecycle.reversed_candidate_id is not None or lifecycle.reversal_event_id is not None:
                return {
                    "provider_lifecycle_id": lifecycle.id,
                    "reason": "posted_provider_lifecycle_shape_invalid",
                }
        elif lifecycle.current_status == "REVERSED":
            if lifecycle.reversed_candidate_id is None:
                return {
                    "provider_lifecycle_id": lifecycle.id,
                    "reason": "reversed_provider_lifecycle_missing_reversed_candidate",
                }
            if (lifecycle.canonical_event_id is None) != (lifecycle.reversal_event_id is None):
                return {
                    "provider_lifecycle_id": lifecycle.id,
                    "reason": "reversed_provider_lifecycle_causal_pair_incomplete",
                }
        else:
            return {
                "provider_lifecycle_id": lifecycle.id,
                "reason": "provider_lifecycle_status_invalid",
            }

        if lifecycle.posted_candidate_id is not None:
            posted = db.query(ProviderNormalizedCandidate).filter(ProviderNormalizedCandidate.id == lifecycle.posted_candidate_id).one_or_none()
            if posted is None or posted.user_id != user_id or posted.external_transaction_record_id != transaction.id or posted.normalized_status != "POSTED":
                return {
                    "provider_lifecycle_id": lifecycle.id,
                    "reason": "provider_lifecycle_posted_candidate_invalid",
                }
        if lifecycle.reversed_candidate_id is not None:
            reversed_candidate = db.query(ProviderNormalizedCandidate).filter(ProviderNormalizedCandidate.id == lifecycle.reversed_candidate_id).one_or_none()
            if reversed_candidate is None or reversed_candidate.user_id != user_id or reversed_candidate.external_transaction_record_id != transaction.id or reversed_candidate.normalized_status != "REVERSED":
                return {
                    "provider_lifecycle_id": lifecycle.id,
                    "reason": "provider_lifecycle_reversed_candidate_invalid",
                }

        interpretation = (
            db.query(ProviderTransactionInterpretation)
            .filter(
                ProviderTransactionInterpretation.external_transaction_record_id == transaction.id,
                ProviderTransactionInterpretation.user_id == user_id,
            )
            .one_or_none()
        )
        if interpretation is None:
            return {
                "provider_lifecycle_id": lifecycle.id,
                "reason": "provider_lifecycle_interpretation_missing",
            }

        if lifecycle.canonical_event_id is not None:
            if interpretation.canonical_event_id != lifecycle.canonical_event_id:
                return {
                    "provider_lifecycle_id": lifecycle.id,
                    "reason": "provider_lifecycle_canonical_interpretation_mismatch",
                }
            original = (
                db.query(FinancialEvent)
                .filter(FinancialEvent.id == lifecycle.canonical_event_id)
                .one_or_none()
            )
            if original is None or original.user_id != user_id or original.provenance != "PROVIDER":
                return {
                    "provider_lifecycle_id": lifecycle.id,
                    "reason": "provider_lifecycle_canonical_event_invalid",
                }

        if lifecycle.reversal_event_id is not None:
            reversal = (
                db.query(FinancialEvent)
                .filter(FinancialEvent.id == lifecycle.reversal_event_id)
                .one_or_none()
            )
            if reversal is None or reversal.user_id != user_id or reversal.event_type != "REVERSAL":
                return {
                    "provider_lifecycle_id": lifecycle.id,
                    "reason": "provider_lifecycle_reversal_event_invalid",
                }
            link = (
                db.query(FinancialEventLink)
                .filter(FinancialEventLink.from_event_id == reversal.id)
                .one_or_none()
            )
            if link is None or link.relation_type != "REVERSAL_OF" or link.to_event_id != lifecycle.canonical_event_id:
                return {
                    "provider_lifecycle_id": lifecycle.id,
                    "reason": "provider_lifecycle_reversal_link_invalid",
                }

    interpretations = (
        db.query(ProviderTransactionInterpretation)
        .filter(ProviderTransactionInterpretation.user_id == user_id)
        .all()
    )
    for interpretation in interpretations:
        candidate = (
            db.query(ProviderNormalizedCandidate)
            .filter(ProviderNormalizedCandidate.id == interpretation.normalized_candidate_id)
            .one()
        )
        lifecycle = (
            db.query(ProviderTransactionLifecycle)
            .filter(
                ProviderTransactionLifecycle.external_transaction_record_id
                == interpretation.external_transaction_record_id
            )
            .one_or_none()
        )
        if candidate.normalized_status in {"PENDING", "POSTED", "REVERSED"} and lifecycle is None:
            return {
                "provider_interpretation_id": interpretation.id,
                "reason": "provider_interpretation_missing_source_lifecycle",
            }

    return None


def find_first_provider_sync_divergence(db: Session, user_id: int) -> dict | None:
    checkpoints = (
        db.query(ProviderSyncCheckpoint)
        .filter(ProviderSyncCheckpoint.user_id == user_id)
        .order_by(ProviderSyncCheckpoint.provider_connection_id.asc())
        .all()
    )
    checkpoint_connection_ids = {row.provider_connection_id for row in checkpoints}

    orphan_pages = (
        db.query(ProviderSyncPage)
        .filter(ProviderSyncPage.user_id == user_id)
        .order_by(ProviderSyncPage.id.asc())
        .all()
    )
    for page in orphan_pages:
        if page.provider_connection_id not in checkpoint_connection_ids:
            return {
                "provider_sync_page_id": page.id,
                "reason": "provider_sync_page_missing_checkpoint",
            }

    for checkpoint in checkpoints:
        connection = (
            db.query(ProviderConnection)
            .filter(ProviderConnection.id == checkpoint.provider_connection_id)
            .one_or_none()
        )
        if connection is None or connection.user_id != user_id:
            return {
                "provider_sync_checkpoint_id": checkpoint.id,
                "reason": "provider_sync_checkpoint_connection_invalid",
            }
        if checkpoint.version < 1:
            return {
                "provider_sync_checkpoint_id": checkpoint.id,
                "reason": "provider_sync_checkpoint_version_invalid",
            }

        pages = (
            db.query(ProviderSyncPage)
            .filter(
                ProviderSyncPage.user_id == user_id,
                ProviderSyncPage.provider_connection_id == connection.id,
            )
            .order_by(ProviderSyncPage.checkpoint_version_after.asc())
            .all()
        )
        if not pages:
            if checkpoint.version != 1 or checkpoint.committed_cursor is not None:
                return {
                    "provider_sync_checkpoint_id": checkpoint.id,
                    "reason": "provider_sync_empty_checkpoint_shape_invalid",
                }
            continue

        expected_version_before = 1
        expected_request_cursor = None
        for page in pages:
            if page.checkpoint_version_before != expected_version_before:
                return {
                    "provider_sync_page_id": page.id,
                    "reason": "provider_sync_checkpoint_version_chain_break",
                    "expected": expected_version_before,
                    "actual": page.checkpoint_version_before,
                }
            if page.checkpoint_version_after != page.checkpoint_version_before + 1:
                return {
                    "provider_sync_page_id": page.id,
                    "reason": "provider_sync_checkpoint_version_step_invalid",
                }
            if page.request_cursor != expected_request_cursor:
                return {
                    "provider_sync_page_id": page.id,
                    "reason": "provider_sync_cursor_chain_break",
                    "expected": expected_request_cursor,
                    "actual": page.request_cursor,
                }
            if page.request_cursor_key != ("<NULL>" if page.request_cursor is None else page.request_cursor):
                return {
                    "provider_sync_page_id": page.id,
                    "reason": "provider_sync_request_cursor_key_invalid",
                }
            if page.has_more and page.next_cursor == page.request_cursor:
                return {
                    "provider_sync_page_id": page.id,
                    "reason": "provider_sync_nonadvancing_cursor_with_more_pages",
                }

            links = (
                db.query(ProviderSyncPageEvidence)
                .filter(ProviderSyncPageEvidence.sync_page_id == page.id)
                .order_by(ProviderSyncPageEvidence.ordinal.asc())
                .all()
            )
            if len(links) != page.observations_count:
                return {
                    "provider_sync_page_id": page.id,
                    "reason": "provider_sync_page_observation_count_mismatch",
                }
            if [row.ordinal for row in links] != list(range(len(links))):
                return {
                    "provider_sync_page_id": page.id,
                    "reason": "provider_sync_page_evidence_ordinal_gap",
                }
            if sum(1 for row in links if row.created_evidence) != page.evidence_created:
                return {
                    "provider_sync_page_id": page.id,
                    "reason": "provider_sync_page_created_count_mismatch",
                }
            if sum(1 for row in links if not row.created_evidence) != page.evidence_deduplicated:
                return {
                    "provider_sync_page_id": page.id,
                    "reason": "provider_sync_page_deduplicated_count_mismatch",
                }

            observations = []
            for link in links:
                evidence = (
                    db.query(ExternalTransactionEvidence)
                    .filter(ExternalTransactionEvidence.id == link.external_evidence_id)
                    .one_or_none()
                )
                if evidence is None or evidence.user_id != user_id or evidence.provider_connection_id != connection.id:
                    return {
                        "provider_sync_page_id": page.id,
                        "reason": "provider_sync_page_evidence_owner_invalid",
                    }
                transaction = (
                    db.query(ExternalTransaction)
                    .filter(ExternalTransaction.id == evidence.external_transaction_record_id)
                    .one_or_none()
                )
                if transaction is None or transaction.user_id != user_id or transaction.provider_connection_id != connection.id:
                    return {
                        "provider_sync_page_id": page.id,
                        "reason": "provider_sync_page_transaction_invalid",
                    }
                observations.append(
                    ProviderSyncObservation(
                        external_transaction_id=transaction.external_transaction_id,
                        observed_at=evidence.observed_at,
                        raw_payload=evidence.raw_payload,
                    )
                )

            expected_hash = sync_page_hash(
                request_cursor=page.request_cursor,
                next_cursor=page.next_cursor,
                has_more=page.has_more,
                observations=observations,
            )
            if page.page_hash != expected_hash:
                return {
                    "provider_sync_page_id": page.id,
                    "reason": "provider_sync_page_hash_mismatch",
                }

            expected_version_before = page.checkpoint_version_after
            expected_request_cursor = page.next_cursor

        if checkpoint.version != pages[-1].checkpoint_version_after:
            return {
                "provider_sync_checkpoint_id": checkpoint.id,
                "reason": "provider_sync_checkpoint_version_not_latest_page",
            }
        if checkpoint.committed_cursor != pages[-1].next_cursor:
            return {
                "provider_sync_checkpoint_id": checkpoint.id,
                "reason": "provider_sync_checkpoint_cursor_not_latest_page",
            }

    return None

def find_first_projection_divergence(db: Session, user_id: int) -> dict | None:
    state = (
        db.query(FinancialProjectionState)
        .filter(FinancialProjectionState.user_id == user_id)
        .one_or_none()
    )
    # A projection is optional derived state. Missing means "not built yet", not
    # corrupted canonical truth. Once a projection exists, every field must be
    # reproducible from canonical state.
    if state is None:
        orphan = (
            db.query(FinancialAccountBalanceProjection.id)
            .filter(FinancialAccountBalanceProjection.user_id == user_id)
            .first()
        )
        if orphan is not None:
            return {
                "reason": "projection_account_rows_without_state",
                "projection_row_id": orphan[0],
            }
        return None

    expected = compute_projection(db, user_id)
    if state.canonical_fingerprint != expected.canonical_fingerprint:
        return {
            "reason": "projection_fingerprint_stale",
            "stored": state.canonical_fingerprint,
            "expected": expected.canonical_fingerprint,
        }
    expected_summary = expected.summary
    scalar_checks = [
        ("total_income", Decimal(state.total_income), expected_summary.total_income),
        ("total_expense", Decimal(state.total_expense), expected_summary.total_expense),
        ("economic_balance", Decimal(state.economic_balance), expected_summary.balance),
        ("net_worth", Decimal(state.net_worth), expected.net_worth),
    ]
    for field, actual, expected_value in scalar_checks:
        if actual != expected_value:
            return {
                "reason": "projection_scalar_mismatch",
                "field": field,
                "actual": str(actual),
                "expected": str(expected_value),
            }

    rows = (
        db.query(FinancialAccountBalanceProjection)
        .filter(FinancialAccountBalanceProjection.user_id == user_id)
        .order_by(FinancialAccountBalanceProjection.account_id.asc())
        .all()
    )
    if state.account_count != len(expected.account_balances):
        return {
            "reason": "projection_account_count_state_mismatch",
            "actual": state.account_count,
            "expected": len(expected.account_balances),
        }
    if len(rows) != len(expected.account_balances):
        return {
            "reason": "projection_account_row_count_mismatch",
            "actual": len(rows),
            "expected": len(expected.account_balances),
        }

    by_account = {row.account_id: row for row in rows}
    for account_id, expected_balance in expected.account_balances.items():
        row = by_account.get(account_id)
        if row is None:
            return {
                "reason": "projection_account_missing",
                "account_id": account_id,
            }
        if row.generation != state.generation:
            return {
                "reason": "projection_generation_mismatch",
                "account_id": account_id,
                "state_generation": state.generation,
                "row_generation": row.generation,
            }
        if Decimal(row.balance) != expected_balance:
            return {
                "reason": "projection_account_balance_mismatch",
                "account_id": account_id,
                "actual": str(row.balance),
                "expected": str(expected_balance),
            }

    projected_net_worth = sum((Decimal(row.balance) for row in rows), Decimal("0"))
    if projected_net_worth != Decimal(state.net_worth):
        return {
            "reason": "projection_net_worth_does_not_equal_account_sum",
            "account_sum": str(projected_net_worth),
            "state_net_worth": str(state.net_worth),
        }
    return None


def audit_user(db: Session, user_id: int) -> dict:
    legacy = legacy_summary(db, user_id)
    canonical_legacy = canonical_legacy_summary(db, user_id)
    economic = canonical_summary(db, user_id)
    first_divergence = find_first_divergence(db, user_id)
    history_divergence = find_first_history_divergence(db, user_id)
    transfer_divergence = find_first_transfer_divergence(db, user_id)
    credit_card_divergence = find_first_credit_card_divergence(db, user_id)
    causal_divergence = find_first_causal_divergence(db, user_id)
    reconciliation_divergence = find_first_reconciliation_divergence(db, user_id)
    provider_evidence_divergence = find_first_provider_evidence_divergence(db, user_id)
    provider_interpretation_divergence = find_first_provider_interpretation_divergence(db, user_id)
    provider_lifecycle_divergence = find_first_provider_lifecycle_divergence(db, user_id)
    provider_sync_divergence = find_first_provider_sync_divergence(db, user_id)
    projection_divergence = find_first_projection_divergence(db, user_id)

    summary_match = legacy == canonical_legacy
    return {
        "user_id": user_id,
        "ok": (
            summary_match
            and first_divergence is None
            and history_divergence is None
            and transfer_divergence is None
            and credit_card_divergence is None
            and causal_divergence is None
            and reconciliation_divergence is None
            and provider_evidence_divergence is None
            and provider_interpretation_divergence is None
            and provider_lifecycle_divergence is None
            and provider_sync_divergence is None
            and projection_divergence is None
        ),
        "legacy": {key: str(value) for key, value in asdict(legacy).items()},
        "canonical": {
            key: str(value) for key, value in asdict(canonical_legacy).items()
        },
        "economic_summary": {
            key: str(value) for key, value in asdict(economic).items()
        },
        "summary_match": summary_match,
        "first_divergence": first_divergence,
        "history_divergence": history_divergence,
        "transfer_divergence": transfer_divergence,
        "credit_card_divergence": credit_card_divergence,
        "causal_divergence": causal_divergence,
        "reconciliation_divergence": reconciliation_divergence,
        "provider_evidence_divergence": provider_evidence_divergence,
        "provider_interpretation_divergence": provider_interpretation_divergence,
        "provider_lifecycle_divergence": provider_lifecycle_divergence,
        "provider_sync_divergence": provider_sync_divergence,
        "projection_divergence": projection_divergence,
    }


def audit_all_users(db: Session) -> list[dict]:
    user_ids = [row[0] for row in db.query(User.id).order_by(User.id.asc()).all()]
    return [audit_user(db, user_id) for user_id in user_ids]
