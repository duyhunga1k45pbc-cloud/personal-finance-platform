from __future__ import annotations

import datetime
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.causal_service import CausalEventError, create_reversal_event
from app.models import (
    ExternalTransaction,
    ExternalTransactionEvidence,
    FinancialEvent,
    FinancialEventLink,
    ProviderNormalizedCandidate,
    ProviderTransactionInterpretation,
    ProviderTransactionLifecycle,
    ProviderTransactionLifecycleHistory,
)


class ProviderLifecycleError(ValueError):
    pass


@dataclass(frozen=True)
class LifecycleObservationResult:
    lifecycle: ProviderTransactionLifecycle | None
    changed: bool
    ignored_stale: bool = False


def _iso_utc(value: datetime.datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.timezone.utc)
    else:
        value = value.astimezone(datetime.timezone.utc)
    return value.isoformat()


def snapshot_provider_lifecycle(lifecycle: ProviderTransactionLifecycle) -> dict:
    return {
        "id": lifecycle.id,
        "user_id": lifecycle.user_id,
        "external_transaction_record_id": lifecycle.external_transaction_record_id,
        "current_status": lifecycle.current_status,
        "current_candidate_id": lifecycle.current_candidate_id,
        "current_observed_at": _iso_utc(lifecycle.current_observed_at),
        "posted_candidate_id": lifecycle.posted_candidate_id,
        "reversed_candidate_id": lifecycle.reversed_candidate_id,
        "canonical_event_id": lifecycle.canonical_event_id,
        "reversal_event_id": lifecycle.reversal_event_id,
        "version": lifecycle.version,
    }


def _append_history(
    db: Session,
    lifecycle: ProviderTransactionLifecycle,
    *,
    transition_type: str,
    source_candidate_id: int,
    previous_state: dict | None,
) -> ProviderTransactionLifecycleHistory:
    row = ProviderTransactionLifecycleHistory(
        lifecycle_id=lifecycle.id,
        user_id=lifecycle.user_id,
        lifecycle_version=lifecycle.version,
        transition_type=transition_type,
        source_candidate_id=source_candidate_id,
        previous_state=previous_state,
        new_state=snapshot_provider_lifecycle(lifecycle),
    )
    db.add(row)
    db.flush()
    return row


def _existing_active_reversal(
    db: Session,
    *,
    original_event_id: int,
) -> FinancialEvent | None:
    rows = (
        db.query(FinancialEvent)
        .join(FinancialEventLink, FinancialEventLink.from_event_id == FinancialEvent.id)
        .filter(
            FinancialEventLink.to_event_id == original_event_id,
            FinancialEventLink.relation_type == "REVERSAL_OF",
            FinancialEvent.lifecycle_state == "ACTIVE",
        )
        .order_by(FinancialEvent.id.asc())
        .all()
    )
    if len(rows) > 1:
        raise ProviderLifecycleError("Original canonical event has multiple active reversals")
    return rows[0] if rows else None


def _has_active_refund(db: Session, *, original_event_id: int) -> bool:
    return (
        db.query(FinancialEvent.id)
        .join(FinancialEventLink, FinancialEventLink.from_event_id == FinancialEvent.id)
        .filter(
            FinancialEventLink.to_event_id == original_event_id,
            FinancialEventLink.relation_type == "REFUND_OF",
            FinancialEvent.lifecycle_state == "ACTIVE",
        )
        .first()
        is not None
    )


def _ensure_reversal(
    db: Session,
    *,
    user_id: int,
    candidate: ProviderNormalizedCandidate,
    canonical_event_id: int,
) -> int:
    existing = _existing_active_reversal(db, original_event_id=canonical_event_id)
    if existing is not None:
        if existing.user_id != user_id:
            raise ProviderLifecycleError("Existing reversal owner mismatch")
        return existing.id

    if _has_active_refund(db, original_event_id=canonical_event_id):
        raise ProviderLifecycleError(
            "Provider reversal cannot be auto-materialized after an active refund; manual causal resolution is required"
        )

    try:
        event = create_reversal_event(
            db,
            user_id=user_id,
            original_event_id=canonical_event_id,
            description=f"Provider reversal of event {canonical_event_id}",
            occurred_at=candidate.occurred_at,
            actor_type="PROVIDER",
            actor_user_id=None,
            reason=f"Provider source lifecycle advanced to REVERSED from normalized candidate {candidate.id}",
            provenance="PROVIDER",
            confidence="OBSERVED",
            interpretation_state="CLASSIFIED",
        )
    except CausalEventError as exc:
        raise ProviderLifecycleError(str(exc)) from exc
    return event.id


def _candidate_observed_at(db: Session, candidate: ProviderNormalizedCandidate) -> datetime.datetime:
    observed_at = (
        db.query(ExternalTransactionEvidence.observed_at)
        .filter(ExternalTransactionEvidence.id == candidate.source_evidence_id)
        .scalar()
    )
    if observed_at is None:
        raise ProviderLifecycleError("Provider lifecycle source evidence is missing")
    if observed_at.tzinfo is None:
        return observed_at.replace(tzinfo=datetime.timezone.utc)
    return observed_at.astimezone(datetime.timezone.utc)


def _validate_transition(current: str, observed: str) -> None:
    allowed = {
        "PENDING": {"PENDING", "POSTED", "REVERSED"},
        "POSTED": {"POSTED", "REVERSED"},
        "REVERSED": {"REVERSED"},
    }
    if observed not in allowed[current]:
        raise ProviderLifecycleError(
            f"Invalid provider lifecycle transition {current} -> {observed}"
        )


def observe_provider_lifecycle(
    db: Session,
    *,
    user_id: int,
    candidate: ProviderNormalizedCandidate,
    interpretation: ProviderTransactionInterpretation,
) -> LifecycleObservationResult:
    """Project normalized provider status into a source lifecycle state machine.

    Raw evidence/candidates remain immutable. This table is the current derived source
    state plus an append-only transition history.
    """

    transaction = (
        db.query(ExternalTransaction)
        .filter(
            ExternalTransaction.id == candidate.external_transaction_record_id,
            ExternalTransaction.user_id == user_id,
        )
        .with_for_update()
        .one_or_none()
    )
    if transaction is None:
        raise ProviderLifecycleError("Provider lifecycle external transaction not found")

    if candidate.user_id != user_id or interpretation.user_id != user_id:
        raise ProviderLifecycleError("Provider lifecycle owner mismatch")
    if candidate.external_transaction_record_id != interpretation.external_transaction_record_id:
        raise ProviderLifecycleError("Provider lifecycle candidate/interpretation transaction mismatch")

    observed = candidate.normalized_status
    if observed == "UNKNOWN":
        return LifecycleObservationResult(None, False)
    if observed not in {"PENDING", "POSTED", "REVERSED"}:
        raise ProviderLifecycleError(f"Unsupported provider lifecycle status {observed}")

    candidate_observed_at = _candidate_observed_at(db, candidate)

    lifecycle = (
        db.query(ProviderTransactionLifecycle)
        .filter(
            ProviderTransactionLifecycle.external_transaction_record_id
            == candidate.external_transaction_record_id
        )
        .with_for_update()
        .one_or_none()
    )

    canonical_event_id = interpretation.canonical_event_id

    if lifecycle is None:
        if observed == "PENDING" and canonical_event_id is not None:
            raise ProviderLifecycleError("PENDING provider transaction cannot have canonical event")
        reversal_event_id = None
        if observed == "REVERSED" and canonical_event_id is not None:
            reversal_event_id = _ensure_reversal(
                db,
                user_id=user_id,
                candidate=candidate,
                canonical_event_id=canonical_event_id,
            )
        lifecycle = ProviderTransactionLifecycle(
            user_id=user_id,
            external_transaction_record_id=candidate.external_transaction_record_id,
            current_status=observed,
            current_candidate_id=candidate.id,
            current_observed_at=candidate_observed_at,
            posted_candidate_id=candidate.id if observed == "POSTED" else None,
            reversed_candidate_id=candidate.id if observed == "REVERSED" else None,
            canonical_event_id=canonical_event_id if observed in {"POSTED", "REVERSED"} else None,
            reversal_event_id=reversal_event_id,
            version=1,
        )
        db.add(lifecycle)
        db.flush()
        _append_history(
            db,
            lifecycle,
            transition_type="INITIALIZED",
            source_candidate_id=candidate.id,
            previous_state=None,
        )
        return LifecycleObservationResult(lifecycle, True)

    if lifecycle.user_id != user_id:
        raise ProviderLifecycleError("Provider lifecycle owner mismatch")

    current_observed_at = lifecycle.current_observed_at
    if current_observed_at.tzinfo is None:
        current_observed_at = current_observed_at.replace(tzinfo=datetime.timezone.utc)
    else:
        current_observed_at = current_observed_at.astimezone(datetime.timezone.utc)

    if candidate_observed_at < current_observed_at:
        # Late normalization of older immutable evidence must never regress source state.
        return LifecycleObservationResult(lifecycle, False, True)
    if candidate_observed_at == current_observed_at and observed != lifecycle.current_status:
        raise ProviderLifecycleError(
            "Conflicting provider lifecycle statuses for the same observed_at"
        )

    _validate_transition(lifecycle.current_status, observed)

    if lifecycle.current_candidate_id == candidate.id:
        if canonical_event_id is not None and lifecycle.canonical_event_id is None:
            return LifecycleObservationResult(
                sync_provider_lifecycle_canonical_link(
                    db,
                    user_id=user_id,
                    interpretation=interpretation,
                    source_candidate_id=candidate.id,
                ),
                True,
            )
        return LifecycleObservationResult(lifecycle, False)

    previous = snapshot_provider_lifecycle(lifecycle)
    status_changed = lifecycle.current_status != observed

    future_canonical_event_id = lifecycle.canonical_event_id
    if canonical_event_id is not None:
        if future_canonical_event_id not in {None, canonical_event_id}:
            raise ProviderLifecycleError("Provider lifecycle canonical event changed unexpectedly")
        future_canonical_event_id = canonical_event_id

    # Create/resolve the causal reversal before mutating lifecycle fields. Otherwise
    # a flush inside causal event creation would expose a transient REVERSED row
    # without its required reversal_event_id and violate the DB state-shape check.
    future_reversal_event_id = lifecycle.reversal_event_id
    if observed == "REVERSED" and future_canonical_event_id is not None and future_reversal_event_id is None:
        future_reversal_event_id = _ensure_reversal(
            db,
            user_id=user_id,
            candidate=candidate,
            canonical_event_id=future_canonical_event_id,
        )

    lifecycle.canonical_event_id = future_canonical_event_id
    lifecycle.reversal_event_id = future_reversal_event_id
    lifecycle.current_status = observed
    lifecycle.current_candidate_id = candidate.id
    lifecycle.current_observed_at = candidate_observed_at
    if observed == "POSTED":
        lifecycle.posted_candidate_id = candidate.id
    elif observed == "REVERSED":
        lifecycle.reversed_candidate_id = candidate.id

    lifecycle.version += 1
    lifecycle.updated_at = datetime.datetime.now(datetime.timezone.utc)
    db.flush()
    _append_history(
        db,
        lifecycle,
        transition_type="ADVANCED" if status_changed else "REFRESHED",
        source_candidate_id=candidate.id,
        previous_state=previous,
    )
    return LifecycleObservationResult(lifecycle, True)


def sync_provider_lifecycle_canonical_link(
    db: Session,
    *,
    user_id: int,
    interpretation: ProviderTransactionInterpretation,
    source_candidate_id: int | None = None,
) -> ProviderTransactionLifecycle | None:
    """Attach a newly materialized canonical event to the already observed POSTED lifecycle."""

    if interpretation.canonical_event_id is None:
        return None
    lifecycle = (
        db.query(ProviderTransactionLifecycle)
        .filter(
            ProviderTransactionLifecycle.external_transaction_record_id
            == interpretation.external_transaction_record_id
        )
        .with_for_update()
        .one_or_none()
    )
    if lifecycle is None:
        return None
    if lifecycle.user_id != user_id:
        raise ProviderLifecycleError("Provider lifecycle owner mismatch")
    if lifecycle.current_status == "PENDING":
        raise ProviderLifecycleError("Cannot link canonical event while provider lifecycle is PENDING")
    if lifecycle.canonical_event_id == interpretation.canonical_event_id:
        return lifecycle
    if lifecycle.canonical_event_id is not None:
        raise ProviderLifecycleError("Provider lifecycle already references a different canonical event")

    previous = snapshot_provider_lifecycle(lifecycle)
    future_reversal_event_id = lifecycle.reversal_event_id
    if lifecycle.current_status == "REVERSED":
        candidate = (
            db.query(ProviderNormalizedCandidate)
            .filter(ProviderNormalizedCandidate.id == lifecycle.current_candidate_id)
            .one()
        )
        future_reversal_event_id = _ensure_reversal(
            db,
            user_id=user_id,
            candidate=candidate,
            canonical_event_id=interpretation.canonical_event_id,
        )
    lifecycle.canonical_event_id = interpretation.canonical_event_id
    lifecycle.reversal_event_id = future_reversal_event_id
    lifecycle.version += 1
    lifecycle.updated_at = datetime.datetime.now(datetime.timezone.utc)
    db.flush()
    _append_history(
        db,
        lifecycle,
        transition_type="CANONICAL_LINKED",
        source_candidate_id=source_candidate_id or lifecycle.current_candidate_id,
        previous_state=previous,
    )
    return lifecycle


def get_provider_lifecycle(
    db: Session,
    *,
    user_id: int,
    transaction_record_id: int,
) -> ProviderTransactionLifecycle | None:
    return (
        db.query(ProviderTransactionLifecycle)
        .filter(
            ProviderTransactionLifecycle.user_id == user_id,
            ProviderTransactionLifecycle.external_transaction_record_id == transaction_record_id,
        )
        .one_or_none()
    )
