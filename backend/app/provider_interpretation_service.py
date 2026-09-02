from __future__ import annotations

import datetime
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.canonical_service import append_financial_event_history, snapshot_financial_event
from app.credit_card_service import validate_transaction_account_semantics
from app.models import (
    ExternalTransaction,
    ExternalTransactionEvidence,
    FinancialAccount,
    FinancialEvent,
    FinancialEventEntry,
    ProviderInterpretationHistory,
    ProviderNormalizedCandidate,
    ProviderTransactionInterpretation,
)


class ProviderInterpretationError(ValueError):
    pass


class StaleInterpretationVersionError(ProviderInterpretationError):
    def __init__(self, *, expected_version: int, current_version: int):
        super().__init__("Provider interpretation was changed by another writer")
        self.expected_version = expected_version
        self.current_version = current_version


@dataclass(frozen=True)
class NormalizeResult:
    candidate: ProviderNormalizedCandidate
    interpretation: ProviderTransactionInterpretation
    candidate_created: bool
    interpretation_locked: bool


def _utc(value: datetime.datetime) -> datetime.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.timezone.utc)
    return value.astimezone(datetime.timezone.utc)


def _event_time(value: datetime.datetime) -> datetime.datetime:
    return _utc(value).replace(tzinfo=None)


def snapshot_provider_interpretation(interp: ProviderTransactionInterpretation) -> dict:
    return {
        "normalized_candidate_id": interp.normalized_candidate_id,
        "state": interp.state,
        "event_type": interp.event_type,
        "account_id": interp.account_id,
        "canonical_event_id": interp.canonical_event_id,
        "confidence": interp.confidence,
        "version": interp.version,
    }


def append_provider_interpretation_history(
    db: Session,
    interp: ProviderTransactionInterpretation,
    *,
    transition_type: str,
    actor_type: str,
    actor_user_id: int | None,
    previous_state: dict | None,
    reason: str | None = None,
) -> ProviderInterpretationHistory:
    row = ProviderInterpretationHistory(
        interpretation_id=interp.id,
        user_id=interp.user_id,
        interpretation_version=interp.version,
        transition_type=transition_type,
        actor_type=actor_type,
        actor_user_id=actor_user_id,
        previous_state=previous_state,
        new_state=snapshot_provider_interpretation(interp),
        reason=reason,
    )
    db.add(row)
    db.flush()
    return row


def _owned_transaction(db: Session, *, user_id: int, transaction_record_id: int) -> ExternalTransaction:
    transaction = (
        db.query(ExternalTransaction)
        .filter(
            ExternalTransaction.id == transaction_record_id,
            ExternalTransaction.user_id == user_id,
        )
        .one_or_none()
    )
    if transaction is None:
        raise ProviderInterpretationError("External transaction not found")
    return transaction


def _owned_evidence(
    db: Session,
    *,
    user_id: int,
    transaction: ExternalTransaction,
    evidence_id: int,
) -> ExternalTransactionEvidence:
    evidence = (
        db.query(ExternalTransactionEvidence)
        .filter(
            ExternalTransactionEvidence.id == evidence_id,
            ExternalTransactionEvidence.user_id == user_id,
            ExternalTransactionEvidence.external_transaction_record_id == transaction.id,
            ExternalTransactionEvidence.provider_connection_id == transaction.provider_connection_id,
        )
        .one_or_none()
    )
    if evidence is None:
        raise ProviderInterpretationError("Source evidence not found for external transaction")
    return evidence


def _candidate_equal(candidate: ProviderNormalizedCandidate, values: dict) -> bool:
    return (
        Decimal(candidate.amount) == values["amount"]
        and candidate.currency == values["currency"]
        and candidate.direction == values["direction"]
        and candidate.normalized_status == values["normalized_status"]
        and _utc(candidate.occurred_at) == values["occurred_at"]
        and candidate.description == values["description"]
        and candidate.provider_status == values["provider_status"]
    )


def _materialization_blocker(candidate: ProviderNormalizedCandidate, event_type: str) -> str | None:
    if candidate.normalized_status == "REVERSED":
        return "PROVIDER_REVERSED_REQUIRES_CAUSAL_RESOLUTION"
    if candidate.normalized_status != "POSTED":
        return "NOT_POSTED"
    if candidate.currency != "VND":
        return "UNSUPPORTED_CURRENCY"
    if event_type not in {"INCOME", "EXPENSE"}:
        return "SPECIALIZED_SEMANTICS_REQUIRED"
    if event_type == "INCOME" and candidate.direction != "INFLOW":
        return "DIRECTION_CONFLICT"
    if event_type == "EXPENSE" and candidate.direction != "OUTFLOW":
        return "DIRECTION_CONFLICT"
    return None


def materialization_blocker(db: Session, interp: ProviderTransactionInterpretation) -> str | None:
    if interp.event_type is None:
        return "UNCLASSIFIED"
    candidate = db.query(ProviderNormalizedCandidate).filter(ProviderNormalizedCandidate.id == interp.normalized_candidate_id).one()
    return _materialization_blocker(candidate, interp.event_type)


def _materialize_simple_event(
    db: Session,
    *,
    interp: ProviderTransactionInterpretation,
    candidate: ProviderNormalizedCandidate,
    actor_user_id: int,
    reason: str | None,
) -> FinancialEvent:
    if interp.event_type not in {"INCOME", "EXPENSE"}:
        raise ProviderInterpretationError("Only provider-backed INCOME/EXPENSE materialize in Task 10")
    blocker = _materialization_blocker(candidate, interp.event_type)
    if blocker is not None:
        raise ProviderInterpretationError(f"Provider interpretation cannot materialize: {blocker}")
    account = (
        db.query(FinancialAccount)
        .filter(
            FinancialAccount.id == interp.account_id,
            FinancialAccount.user_id == interp.user_id,
        )
        .one_or_none()
    )
    if account is None:
        raise ProviderInterpretationError("Financial account not found for user")
    if account.currency != "VND":
        raise ProviderInterpretationError("V1 canonical provider events require VND account")
    validate_transaction_account_semantics(account, interp.event_type.lower())

    amount = Decimal(candidate.amount)
    signed = amount if interp.event_type == "INCOME" else -amount
    event = FinancialEvent(
        user_id=interp.user_id,
        legacy_transaction_id=None,
        event_type=interp.event_type,
        description=candidate.description,
        category=None,
        occurred_at=_event_time(candidate.occurred_at),
        effective_at=_event_time(candidate.occurred_at),
        interpretation_state="USER_CONFIRMED",
        provenance="PROVIDER",
        confidence="USER_CONFIRMED",
        lifecycle_state="ACTIVE",
        version=1,
    )
    db.add(event)
    db.flush()
    db.add(FinancialEventEntry(financial_event_id=event.id, account_id=account.id, amount=signed))
    db.flush()
    append_financial_event_history(
        db,
        event,
        transition_type="CREATED",
        actor_type="USER",
        actor_user_id=actor_user_id,
        previous_state=None,
        new_state=snapshot_financial_event(db, event),
        reason=reason or "Provider transaction interpretation confirmed",
    )
    return event


def normalize_external_transaction(
    db: Session,
    *,
    user_id: int,
    transaction_record_id: int,
    evidence_id: int,
    normalizer_version: str,
    amount: Decimal,
    currency: str,
    direction: str,
    normalized_status: str,
    occurred_at: datetime.datetime,
    description: str | None,
    provider_status: str | None,
) -> NormalizeResult:
    transaction = _owned_transaction(db, user_id=user_id, transaction_record_id=transaction_record_id)
    _owned_evidence(db, user_id=user_id, transaction=transaction, evidence_id=evidence_id)

    version_name = normalizer_version.strip()
    if not version_name:
        raise ProviderInterpretationError("normalizer_version must not be blank")
    amount = Decimal(amount)
    if amount <= 0:
        raise ProviderInterpretationError("Normalized amount must be positive")
    currency = currency.strip().upper()
    if len(currency) != 3:
        raise ProviderInterpretationError("Normalized currency must be a 3-letter code")
    direction = direction.upper()
    normalized_status = normalized_status.upper()
    if direction not in {"INFLOW", "OUTFLOW"}:
        raise ProviderInterpretationError("Normalized direction must be INFLOW or OUTFLOW")
    if normalized_status not in {"PENDING", "POSTED", "REVERSED", "UNKNOWN"}:
        raise ProviderInterpretationError("Unsupported normalized provider status")

    values = {
        "amount": amount,
        "currency": currency,
        "direction": direction,
        "normalized_status": normalized_status,
        "occurred_at": _utc(occurred_at),
        "description": description,
        "provider_status": provider_status,
    }

    candidate = (
        db.query(ProviderNormalizedCandidate)
        .filter(
            ProviderNormalizedCandidate.source_evidence_id == evidence_id,
            ProviderNormalizedCandidate.normalizer_version == version_name,
        )
        .one_or_none()
    )
    candidate_created = False
    if candidate is not None:
        if not _candidate_equal(candidate, values):
            raise ProviderInterpretationError(
                "Same evidence + normalizer_version produced different normalized output"
            )
    else:
        candidate = ProviderNormalizedCandidate(
            user_id=user_id,
            provider_connection_id=transaction.provider_connection_id,
            external_transaction_record_id=transaction.id,
            source_evidence_id=evidence_id,
            normalizer_version=version_name,
            **values,
        )
        try:
            with db.begin_nested():
                db.add(candidate)
                db.flush()
            candidate_created = True
        except IntegrityError:
            candidate = (
                db.query(ProviderNormalizedCandidate)
                .filter(
                    ProviderNormalizedCandidate.source_evidence_id == evidence_id,
                    ProviderNormalizedCandidate.normalizer_version == version_name,
                )
                .one()
            )
            if not _candidate_equal(candidate, values):
                raise ProviderInterpretationError(
                    "Concurrent normalization disagreed for the same evidence + normalizer_version"
                )

    interp = (
        db.query(ProviderTransactionInterpretation)
        .filter(ProviderTransactionInterpretation.external_transaction_record_id == transaction.id)
        .with_for_update()
        .one_or_none()
    )
    if interp is None:
        interp = ProviderTransactionInterpretation(
            user_id=user_id,
            external_transaction_record_id=transaction.id,
            normalized_candidate_id=candidate.id,
            state="UNCLASSIFIED",
            event_type=None,
            account_id=None,
            canonical_event_id=None,
            confidence=None,
            version=1,
        )
        db.add(interp)
        db.flush()
        append_provider_interpretation_history(
            db,
            interp,
            transition_type="NORMALIZED",
            actor_type="SYSTEM",
            actor_user_id=None,
            previous_state=None,
            reason="Initial normalization from immutable provider evidence",
        )
        return NormalizeResult(candidate, interp, candidate_created, False)

    if interp.user_id != user_id:
        raise ProviderInterpretationError("Provider interpretation owner mismatch")
    if interp.canonical_event_id is not None:
        # New evidence is preserved, but already-materialized user-confirmed semantics
        # are never silently rebased or rewritten.
        return NormalizeResult(candidate, interp, candidate_created, True)
    if interp.normalized_candidate_id == candidate.id:
        return NormalizeResult(candidate, interp, candidate_created, interp.state == "USER_CONFIRMED")

    previous = snapshot_provider_interpretation(interp)
    interp.normalized_candidate_id = candidate.id
    transition = "NORMALIZED"
    if interp.state != "USER_CONFIRMED":
        interp.state = "UNCLASSIFIED"
        interp.event_type = None
        interp.account_id = None
        interp.confidence = None
    interp.version += 1
    interp.updated_at = datetime.datetime.now(datetime.timezone.utc)
    db.flush()

    if interp.state == "USER_CONFIRMED" and _materialization_blocker(candidate, interp.event_type) is None:
        event = _materialize_simple_event(
            db,
            interp=interp,
            candidate=candidate,
            actor_user_id=interp.user_id,
            reason="Previously confirmed interpretation became materializable after new provider evidence",
        )
        interp.canonical_event_id = event.id
        db.flush()
        transition = "MATERIALIZED"

    append_provider_interpretation_history(
        db,
        interp,
        transition_type=transition,
        actor_type="SYSTEM",
        actor_user_id=None,
        previous_state=previous,
        reason="Normalized candidate changed from newer immutable evidence",
    )
    return NormalizeResult(candidate, interp, candidate_created, False)


def _claim_version(db: Session, *, interp_id: int, user_id: int, expected_version: int) -> ProviderTransactionInterpretation:
    updated = (
        db.query(ProviderTransactionInterpretation)
        .filter(
            ProviderTransactionInterpretation.id == interp_id,
            ProviderTransactionInterpretation.user_id == user_id,
            ProviderTransactionInterpretation.version == expected_version,
        )
        .update(
            {
                ProviderTransactionInterpretation.version: ProviderTransactionInterpretation.version + 1,
                ProviderTransactionInterpretation.updated_at: datetime.datetime.now(datetime.timezone.utc),
            },
            synchronize_session=False,
        )
    )
    if updated != 1:
        current = (
            db.query(ProviderTransactionInterpretation.version)
            .filter(
                ProviderTransactionInterpretation.id == interp_id,
                ProviderTransactionInterpretation.user_id == user_id,
            )
            .scalar()
        )
        if current is None:
            raise ProviderInterpretationError("Provider interpretation not found")
        raise StaleInterpretationVersionError(expected_version=expected_version, current_version=current)
    db.flush()
    db.expire_all()
    return (
        db.query(ProviderTransactionInterpretation)
        .filter(ProviderTransactionInterpretation.id == interp_id)
        .one()
    )


def classify_external_transaction(
    db: Session,
    *,
    user_id: int,
    transaction_record_id: int,
    expected_version: int,
    event_type: str,
    reason: str | None,
) -> ProviderTransactionInterpretation:
    interp = (
        db.query(ProviderTransactionInterpretation)
        .filter(
            ProviderTransactionInterpretation.user_id == user_id,
            ProviderTransactionInterpretation.external_transaction_record_id == transaction_record_id,
        )
        .one_or_none()
    )
    if interp is None:
        raise ProviderInterpretationError("Normalize provider evidence before classification")
    if interp.state == "USER_CONFIRMED":
        raise ProviderInterpretationError("System classification cannot overwrite user-confirmed interpretation")
    event_type = event_type.upper()
    if event_type not in {"INCOME", "EXPENSE", "TRANSFER", "REFUND", "REVERSAL"}:
        raise ProviderInterpretationError("Unsupported provider event classification")
    previous = snapshot_provider_interpretation(interp)
    interp = _claim_version(db, interp_id=interp.id, user_id=user_id, expected_version=expected_version)
    interp.state = "CLASSIFIED"
    interp.event_type = event_type
    interp.account_id = None
    interp.canonical_event_id = None
    interp.confidence = "INFERRED"
    db.flush()
    append_provider_interpretation_history(
        db,
        interp,
        transition_type="CLASSIFIED",
        actor_type="SYSTEM",
        actor_user_id=None,
        previous_state=previous,
        reason=reason,
    )
    return interp


def confirm_external_interpretation(
    db: Session,
    *,
    user_id: int,
    transaction_record_id: int,
    expected_version: int,
    event_type: str,
    account_id: int,
    actor_user_id: int,
    reason: str | None,
) -> ProviderTransactionInterpretation:
    interp = (
        db.query(ProviderTransactionInterpretation)
        .filter(
            ProviderTransactionInterpretation.user_id == user_id,
            ProviderTransactionInterpretation.external_transaction_record_id == transaction_record_id,
        )
        .one_or_none()
    )
    if interp is None:
        raise ProviderInterpretationError("Normalize provider evidence before confirmation")
    if interp.state == "USER_CONFIRMED":
        raise ProviderInterpretationError("Provider interpretation is already user-confirmed")
    event_type = event_type.upper()
    if event_type not in {"INCOME", "EXPENSE", "TRANSFER", "REFUND", "REVERSAL"}:
        raise ProviderInterpretationError("Unsupported provider event classification")
    account = (
        db.query(FinancialAccount)
        .filter(FinancialAccount.id == account_id, FinancialAccount.user_id == user_id)
        .one_or_none()
    )
    if account is None:
        raise ProviderInterpretationError("Financial account not found for user")

    candidate = db.query(ProviderNormalizedCandidate).filter(ProviderNormalizedCandidate.id == interp.normalized_candidate_id).one()
    blocker = _materialization_blocker(candidate, event_type)
    if blocker == "DIRECTION_CONFLICT":
        raise ProviderInterpretationError("Confirmed INCOME/EXPENSE conflicts with normalized money direction")
    if event_type == "INCOME":
        validate_transaction_account_semantics(account, "income")

    previous = snapshot_provider_interpretation(interp)
    interp = _claim_version(db, interp_id=interp.id, user_id=user_id, expected_version=expected_version)
    interp.state = "USER_CONFIRMED"
    interp.event_type = event_type
    interp.account_id = account.id
    interp.confidence = "USER_CONFIRMED"
    transition = "USER_CONFIRMED"

    if blocker is None:
        event = _materialize_simple_event(
            db,
            interp=interp,
            candidate=candidate,
            actor_user_id=actor_user_id,
            reason=reason,
        )
        interp.canonical_event_id = event.id
        transition = "MATERIALIZED"
    db.flush()
    append_provider_interpretation_history(
        db,
        interp,
        transition_type=transition,
        actor_type="USER",
        actor_user_id=actor_user_id,
        previous_state=previous,
        reason=reason,
    )
    return interp
