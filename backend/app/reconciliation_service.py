from __future__ import annotations

import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.canonical_service import append_financial_event_history, snapshot_financial_event
from app.credit_card_service import canonical_account_position
from app.observability import increment_metric, log_event
from app.models import (
    FinancialAccount,
    FinancialEvent,
    FinancialEventEntry,
    ReconciliationCase,
    ReconciliationHistory,
)


class ReconciliationError(ValueError):
    pass


class ReconciliationNotFoundError(ReconciliationError):
    pass


class ReconciliationConflictError(ReconciliationError):
    pass


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _naive_utc(value: datetime.datetime) -> datetime.datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(datetime.timezone.utc).replace(tzinfo=None)


def _snapshot_money(value) -> str:
    return format(Decimal(value).quantize(Decimal("0.01")), "f")


def _snapshot_time(value: datetime.datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return value.isoformat()


def snapshot_reconciliation(case: ReconciliationCase) -> dict:
    # Normalize representation so audit snapshots are stable across PostgreSQL
    # and SQLite (timezone-aware datetime + NUMERIC formatting differ on reload).
    return {
        "id": case.id,
        "user_id": case.user_id,
        "account_id": case.account_id,
        "expected_balance": _snapshot_money(case.expected_balance),
        "observed_balance": _snapshot_money(case.observed_balance),
        "difference": _snapshot_money(case.difference),
        "status": case.status,
        "resolution_type": case.resolution_type,
        "adjustment_event_id": case.adjustment_event_id,
        "resolved_balance": (
            _snapshot_money(case.resolved_balance)
            if case.resolved_balance is not None
            else None
        ),
        "note": case.note,
        "version": case.version,
        "observed_at": _snapshot_time(case.observed_at),
        "resolved_at": _snapshot_time(case.resolved_at),
    }


def append_reconciliation_history(
    db: Session,
    case: ReconciliationCase,
    *,
    transition_type: str,
    actor_type: str,
    actor_user_id: int | None,
    previous_state: dict | None,
    new_state: dict,
    reason: str | None,
) -> ReconciliationHistory:
    row = ReconciliationHistory(
        reconciliation_id=case.id,
        user_id=case.user_id,
        reconciliation_version=case.version,
        transition_type=transition_type,
        actor_type=actor_type,
        actor_user_id=actor_user_id,
        previous_state=previous_state,
        new_state=new_state,
        reason=reason,
    )
    db.add(row)
    db.flush()
    return row


def get_owned_reconciliation(
    db: Session,
    *,
    user_id: int,
    reconciliation_id: int,
) -> ReconciliationCase:
    case = (
        db.query(ReconciliationCase)
        .filter(
            ReconciliationCase.id == reconciliation_id,
            ReconciliationCase.user_id == user_id,
        )
        .one_or_none()
    )
    if case is None:
        raise ReconciliationNotFoundError("Reconciliation not found")
    return case


def _get_owned_cash_account(
    db: Session,
    *,
    user_id: int,
    account_id: int,
) -> FinancialAccount:
    account = (
        db.query(FinancialAccount)
        .filter(
            FinancialAccount.id == account_id,
            FinancialAccount.user_id == user_id,
        )
        .one_or_none()
    )
    if account is None:
        raise ReconciliationNotFoundError("Account not found")
    if account.account_type != "CASH":
        raise ReconciliationError(
            "Task 8 reconciliation supports CASH accounts only; provider-observed bank/card balances are deferred to the provider layer"
        )
    return account


def detect_cash_reconciliation(
    db: Session,
    *,
    user_id: int,
    account_id: int,
    observed_balance: Decimal,
    observed_at: datetime.datetime | None = None,
    note: str | None = None,
    actor_user_id: int | None = None,
) -> ReconciliationCase:
    _get_owned_cash_account(db, user_id=user_id, account_id=account_id)
    observed = Decimal(observed_balance)
    if observed < 0:
        raise ReconciliationError("Observed cash balance cannot be negative")

    expected = canonical_account_position(
        db,
        user_id=user_id,
        account_id=account_id,
    )
    difference = observed - expected
    status = "RECONCILED" if difference == 0 else "MISMATCH"

    case = ReconciliationCase(
        user_id=user_id,
        account_id=account_id,
        expected_balance=expected,
        observed_balance=observed,
        difference=difference,
        status=status,
        resolution_type=None,
        adjustment_event_id=None,
        resolved_balance=None,
        note=note,
        version=1,
        observed_at=observed_at or _utc_now(),
    )
    db.add(case)
    db.flush()
    append_reconciliation_history(
        db,
        case,
        transition_type="DETECTED",
        actor_type="USER",
        actor_user_id=actor_user_id,
        previous_state=None,
        new_state=snapshot_reconciliation(case),
        reason="cash_balance_observed",
    )
    if status == "MISMATCH":
        increment_metric("reconciliation_mismatches_detected_total")
        log_event(
            "reconciliation.mismatch_detected",
            reconciliation_id=case.id,
            account_id=case.account_id,
        )
    else:
        increment_metric("reconciliation_matches_detected_total")
    return case


def _claim_mismatch_version(
    db: Session,
    *,
    case: ReconciliationCase,
    expected_version: int,
) -> None:
    if expected_version < 1:
        raise ReconciliationConflictError("Expected version must be positive")
    if case.version != expected_version:
        raise ReconciliationConflictError(
            f"Stale reconciliation version: expected {expected_version}, current {case.version}"
        )
    if case.status != "MISMATCH":
        raise ReconciliationConflictError(
            f"Reconciliation is not an unresolved mismatch (status={case.status})"
        )

    updated = (
        db.query(ReconciliationCase)
        .filter(
            ReconciliationCase.id == case.id,
            ReconciliationCase.user_id == case.user_id,
            ReconciliationCase.version == expected_version,
            ReconciliationCase.status == "MISMATCH",
        )
        .update(
            {ReconciliationCase.version: expected_version + 1},
            synchronize_session=False,
        )
    )
    if updated != 1:
        raise ReconciliationConflictError(
            "Reconciliation was changed by another writer"
        )
    db.flush()
    db.expire(case)
    db.refresh(case)


def resolve_reconciliation_with_real_events(
    db: Session,
    *,
    user_id: int,
    reconciliation_id: int,
    expected_version: int,
    reason: str | None,
    actor_user_id: int | None,
) -> ReconciliationCase:
    case = get_owned_reconciliation(
        db,
        user_id=user_id,
        reconciliation_id=reconciliation_id,
    )
    previous_state = snapshot_reconciliation(case)

    current_expected = canonical_account_position(
        db,
        user_id=user_id,
        account_id=case.account_id,
    )
    if current_expected != Decimal(case.observed_balance):
        raise ReconciliationConflictError(
            "Reconciliation mismatch still exists; record the real missing financial event or explicitly confirm an adjustment"
        )

    _claim_mismatch_version(db, case=case, expected_version=expected_version)
    case.status = "RESOLVED"
    case.resolution_type = "REAL_EVENT"
    case.adjustment_event_id = None
    case.resolved_balance = current_expected
    case.resolved_at = _utc_now()
    db.flush()
    append_reconciliation_history(
        db,
        case,
        transition_type="RESOLVED_REAL_EVENT",
        actor_type="USER",
        actor_user_id=actor_user_id,
        previous_state=previous_state,
        new_state=snapshot_reconciliation(case),
        reason=reason or "resolved_by_recorded_financial_events",
    )
    increment_metric("reconciliation_resolved_real_event_total")
    log_event(
        "reconciliation.resolved",
        reconciliation_id=case.id,
        resolution_type="REAL_EVENT",
    )
    return case


def confirm_reconciliation_adjustment(
    db: Session,
    *,
    user_id: int,
    reconciliation_id: int,
    expected_version: int,
    reason: str,
    actor_user_id: int | None,
) -> tuple[ReconciliationCase, FinancialEvent]:
    reason = reason.strip()
    if not reason:
        raise ReconciliationError("Adjustment confirmation requires a reason")

    case = get_owned_reconciliation(
        db,
        user_id=user_id,
        reconciliation_id=reconciliation_id,
    )
    previous_state = snapshot_reconciliation(case)

    current_expected = canonical_account_position(
        db,
        user_id=user_id,
        account_id=case.account_id,
    )
    if current_expected != Decimal(case.expected_balance):
        raise ReconciliationConflictError(
            "Reconciliation is stale because canonical account state changed after observation; create a new reconciliation or resolve this one with real events"
        )
    difference = Decimal(case.observed_balance) - current_expected
    if difference == 0:
        raise ReconciliationConflictError(
            "No adjustment is required because expected and observed balances already match"
        )

    _claim_mismatch_version(db, case=case, expected_version=expected_version)

    effective_time = _naive_utc(case.observed_at or _utc_now())
    event = FinancialEvent(
        user_id=user_id,
        legacy_transaction_id=None,
        event_type="ADJUSTMENT",
        description=reason,
        category="reconciliation_adjustment",
        occurred_at=effective_time,
        effective_at=effective_time,
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
            account_id=case.account_id,
            amount=difference,
        )
    )
    db.flush()
    append_financial_event_history(
        db,
        event,
        transition_type="CREATED",
        actor_type="USER",
        actor_user_id=actor_user_id,
        previous_state=None,
        new_state=snapshot_financial_event(db, event),
        reason=f"reconciliation_adjustment:{case.id}:{reason}",
    )

    resolved_balance = canonical_account_position(
        db,
        user_id=user_id,
        account_id=case.account_id,
    )
    if resolved_balance != Decimal(case.observed_balance):
        raise ReconciliationConflictError(
            "Adjustment did not close the reconciliation mismatch"
        )

    case.status = "RESOLVED"
    case.resolution_type = "ADJUSTMENT"
    case.adjustment_event_id = event.id
    case.resolved_balance = resolved_balance
    case.resolved_at = _utc_now()
    db.flush()
    append_reconciliation_history(
        db,
        case,
        transition_type="RESOLVED_ADJUSTMENT",
        actor_type="USER",
        actor_user_id=actor_user_id,
        previous_state=previous_state,
        new_state=snapshot_reconciliation(case),
        reason=reason,
    )
    increment_metric("reconciliation_resolved_adjustment_total")
    log_event(
        "reconciliation.resolved",
        reconciliation_id=case.id,
        resolution_type="ADJUSTMENT",
    )
    return case, event
