from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from app.command_service import (
    IdempotencyConflictError,
    add_command_receipt,
    commit_with_idempotency_race_recovery,
    get_stored_command_response,
    hash_command,
    normalize_idempotency_key,
)
from app.database import SessionLocal
from app.models import ExternalTransaction, ExternalTransactionEvidence, ProviderConnection, ProviderTransactionInterpretation, User
from app.provider_service import (
    get_or_create_provider_connection,
    get_owned_provider_connection,
    ingest_external_evidence,
)
from app.provider_interpretation_service import (
    ProviderInterpretationError,
    StaleInterpretationVersionError,
    classify_external_transaction,
    confirm_external_interpretation,
    materialization_blocker,
    normalize_external_transaction,
)
from app.routers.auth import get_current_user
from app.schemas import (
    ExternalTransactionEvidenceCreate,
    ProviderClassificationCreate,
    ProviderConnectionCreate,
    ProviderInterpretationConfirm,
    ProviderNormalizationCreate,
)

router = APIRouter(prefix="/provider-connections", tags=["provider-evidence"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _require_key(value: str | None) -> str:
    if value is None:
        raise HTTPException(status_code=400, detail="Idempotency-Key header is required")
    try:
        return normalize_idempotency_key(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _stored_or_conflict(db, *, user_id, command_type, key, request_hash):
    try:
        return get_stored_command_response(
            db,
            user_id=user_id,
            command_type=command_type,
            idempotency_key=key,
            request_hash=request_hash,
        )
    except IdempotencyConflictError as exc:
        raise HTTPException(status_code=409, detail={"code": "idempotency_conflict", "message": str(exc)}) from exc


def _connection_body(connection: ProviderConnection, *, deduplicated: bool) -> dict:
    return {
        "id": connection.id,
        "provider_name": connection.provider_name,
        "external_account_id": connection.external_account_id,
        "display_name": connection.display_name,
        "deduplicated": deduplicated,
    }


def _evidence_body(result) -> dict:
    return {
        "external_transaction_record_id": result.transaction.id,
        "external_transaction_id": result.transaction.external_transaction_id,
        "evidence_id": result.evidence.id,
        "observed_at": result.evidence.observed_at,
        "payload_sha256": result.evidence.payload_sha256,
        "deduplicated": not result.created_evidence,
        "canonicalized": False,
    }


@router.post("")
def create_provider_connection(
    payload: ProviderConnectionCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    key = _require_key(idempotency_key)
    request_payload = payload.model_dump(mode="json")
    request_hash = hash_command(request_payload)
    stored = _stored_or_conflict(
        db,
        user_id=current_user.id,
        command_type="CREATE_PROVIDER_CONNECTION",
        key=key,
        request_hash=request_hash,
    )
    if stored is not None:
        return stored.body

    try:
        result = get_or_create_provider_connection(
            db,
            user_id=current_user.id,
            provider_name=payload.provider_name,
            external_account_id=payload.external_account_id,
            display_name=payload.display_name,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    body = _connection_body(result.connection, deduplicated=not result.created)
    add_command_receipt(
        db,
        user_id=current_user.id,
        command_type="CREATE_PROVIDER_CONNECTION",
        idempotency_key=key,
        request_hash=request_hash,
        provider_connection_id=result.connection.id,
        response_status=200,
        response_body=body,
    )
    raced = commit_with_idempotency_race_recovery(
        db,
        user_id=current_user.id,
        command_type="CREATE_PROVIDER_CONNECTION",
        idempotency_key=key,
        request_hash=request_hash,
    )
    return raced.body if raced is not None else body


@router.get("")
def list_provider_connections(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rows = (
        db.query(ProviderConnection)
        .filter(ProviderConnection.user_id == current_user.id)
        .order_by(ProviderConnection.id.asc())
        .all()
    )
    return [_connection_body(row, deduplicated=False) for row in rows]


@router.get("/{connection_id}")
def get_provider_connection(
    connection_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    connection = get_owned_provider_connection(
        db,
        user_id=current_user.id,
        connection_id=connection_id,
    )
    if connection is None:
        raise HTTPException(status_code=404, detail="Provider connection not found")
    return _connection_body(connection, deduplicated=False)


@router.post("/{connection_id}/external-transactions")
def ingest_provider_transaction(
    connection_id: int,
    payload: ExternalTransactionEvidenceCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    connection = get_owned_provider_connection(
        db,
        user_id=current_user.id,
        connection_id=connection_id,
    )
    if connection is None:
        raise HTTPException(status_code=404, detail="Provider connection not found")

    key = _require_key(idempotency_key)
    request_payload = {
        "connection_id": connection_id,
        **payload.model_dump(mode="json"),
    }
    request_hash = hash_command(request_payload)
    stored = _stored_or_conflict(
        db,
        user_id=current_user.id,
        command_type="INGEST_EXTERNAL_EVIDENCE",
        key=key,
        request_hash=request_hash,
    )
    if stored is not None:
        return stored.body

    try:
        result = ingest_external_evidence(
            db,
            user_id=current_user.id,
            connection=connection,
            external_transaction_id=payload.external_transaction_id,
            raw_payload=payload.raw_payload,
            observed_at=payload.observed_at,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    body = _evidence_body(result)
    add_command_receipt(
        db,
        user_id=current_user.id,
        command_type="INGEST_EXTERNAL_EVIDENCE",
        idempotency_key=key,
        request_hash=request_hash,
        external_evidence_id=result.evidence.id,
        response_status=200,
        response_body=body,
    )
    raced = commit_with_idempotency_race_recovery(
        db,
        user_id=current_user.id,
        command_type="INGEST_EXTERNAL_EVIDENCE",
        idempotency_key=key,
        request_hash=request_hash,
    )
    return raced.body if raced is not None else body


@router.get("/{connection_id}/external-transactions")
def list_external_transactions(
    connection_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    connection = get_owned_provider_connection(
        db,
        user_id=current_user.id,
        connection_id=connection_id,
    )
    if connection is None:
        raise HTTPException(status_code=404, detail="Provider connection not found")

    rows = (
        db.query(ExternalTransaction)
        .filter(ExternalTransaction.provider_connection_id == connection.id)
        .order_by(ExternalTransaction.id.asc())
        .all()
    )
    return [
        {
            "id": row.id,
            "external_transaction_id": row.external_transaction_id,
            "first_observed_at": row.first_observed_at,
            "evidence_count": db.query(ExternalTransactionEvidence).filter(
                ExternalTransactionEvidence.external_transaction_record_id == row.id
            ).count(),
        }
        for row in rows
    ]


@router.get("/{connection_id}/external-transactions/{transaction_record_id}/evidence")
def list_external_transaction_evidence(
    connection_id: int,
    transaction_record_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    connection = get_owned_provider_connection(
        db,
        user_id=current_user.id,
        connection_id=connection_id,
    )
    if connection is None:
        raise HTTPException(status_code=404, detail="Provider connection not found")

    transaction = (
        db.query(ExternalTransaction)
        .filter(
            ExternalTransaction.id == transaction_record_id,
            ExternalTransaction.provider_connection_id == connection.id,
            ExternalTransaction.user_id == current_user.id,
        )
        .one_or_none()
    )
    if transaction is None:
        raise HTTPException(status_code=404, detail="External transaction not found")

    rows = (
        db.query(ExternalTransactionEvidence)
        .filter(ExternalTransactionEvidence.external_transaction_record_id == transaction.id)
        .order_by(ExternalTransactionEvidence.observed_at.asc(), ExternalTransactionEvidence.id.asc())
        .all()
    )
    return [
        {
            "id": row.id,
            "observed_at": row.observed_at,
            "recorded_at": row.recorded_at,
            "payload_sha256": row.payload_sha256,
            "raw_payload": row.raw_payload,
        }
        for row in rows
    ]


def _expected_version(value: int | None) -> int:
    if value is None or value < 1:
        raise HTTPException(status_code=400, detail="X-Expected-Version header is required and must be >= 1")
    return value


def _interpretation_body(db: Session, interp: ProviderTransactionInterpretation) -> dict:
    return {
        "id": interp.id,
        "external_transaction_record_id": interp.external_transaction_record_id,
        "normalized_candidate_id": interp.normalized_candidate_id,
        "state": interp.state,
        "event_type": interp.event_type,
        "account_id": interp.account_id,
        "canonical_event_id": interp.canonical_event_id,
        "confidence": interp.confidence,
        "version": interp.version,
        "materialization_blocker": materialization_blocker(db, interp),
    }


def _transaction_for_connection(db: Session, *, user_id: int, connection_id: int, transaction_record_id: int) -> ExternalTransaction:
    row = (
        db.query(ExternalTransaction)
        .filter(
            ExternalTransaction.id == transaction_record_id,
            ExternalTransaction.user_id == user_id,
            ExternalTransaction.provider_connection_id == connection_id,
        )
        .one_or_none()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="External transaction not found")
    return row


@router.post("/{connection_id}/external-transactions/{transaction_record_id}/normalizations")
def normalize_provider_transaction(
    connection_id: int,
    transaction_record_id: int,
    payload: ProviderNormalizationCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    connection = get_owned_provider_connection(db, user_id=current_user.id, connection_id=connection_id)
    if connection is None:
        raise HTTPException(status_code=404, detail="Provider connection not found")
    _transaction_for_connection(db, user_id=current_user.id, connection_id=connection.id, transaction_record_id=transaction_record_id)
    key = _require_key(idempotency_key)
    request_payload = {"connection_id": connection_id, "transaction_record_id": transaction_record_id, **payload.model_dump(mode="json")}
    request_hash = hash_command(request_payload)
    stored = _stored_or_conflict(db, user_id=current_user.id, command_type="NORMALIZE_EXTERNAL_TRANSACTION", key=key, request_hash=request_hash)
    if stored is not None:
        return stored.body
    try:
        result = normalize_external_transaction(
            db,
            user_id=current_user.id,
            transaction_record_id=transaction_record_id,
            evidence_id=payload.evidence_id,
            normalizer_version=payload.normalizer_version,
            amount=payload.amount,
            currency=payload.currency,
            direction=payload.direction,
            normalized_status=payload.normalized_status,
            occurred_at=payload.occurred_at,
            description=payload.description,
            provider_status=payload.provider_status,
        )
    except ProviderInterpretationError as exc:
        db.rollback()
        status = 409 if "same evidence" in str(exc).lower() or "concurrent normalization" in str(exc).lower() else 422
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    body = {
        "candidate": {
            "id": result.candidate.id,
            "evidence_id": result.candidate.source_evidence_id,
            "amount": result.candidate.amount,
            "currency": result.candidate.currency,
            "direction": result.candidate.direction,
            "normalized_status": result.candidate.normalized_status,
            "normalizer_version": result.candidate.normalizer_version,
        },
        "interpretation": _interpretation_body(db, result.interpretation),
        "candidate_deduplicated": not result.candidate_created,
        "interpretation_locked": result.interpretation_locked,
    }
    add_command_receipt(
        db,
        user_id=current_user.id,
        command_type="NORMALIZE_EXTERNAL_TRANSACTION",
        idempotency_key=key,
        request_hash=request_hash,
        normalized_candidate_id=result.candidate.id,
        response_status=200,
        response_body=body,
    )
    raced = commit_with_idempotency_race_recovery(db, user_id=current_user.id, command_type="NORMALIZE_EXTERNAL_TRANSACTION", idempotency_key=key, request_hash=request_hash)
    return raced.body if raced is not None else body


@router.get("/{connection_id}/external-transactions/{transaction_record_id}/interpretation")
def get_provider_interpretation(
    connection_id: int,
    transaction_record_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    connection = get_owned_provider_connection(db, user_id=current_user.id, connection_id=connection_id)
    if connection is None:
        raise HTTPException(status_code=404, detail="Provider connection not found")
    _transaction_for_connection(db, user_id=current_user.id, connection_id=connection.id, transaction_record_id=transaction_record_id)
    interp = (
        db.query(ProviderTransactionInterpretation)
        .filter(
            ProviderTransactionInterpretation.user_id == current_user.id,
            ProviderTransactionInterpretation.external_transaction_record_id == transaction_record_id,
        )
        .one_or_none()
    )
    if interp is None:
        raise HTTPException(status_code=404, detail="Provider interpretation not found")
    return _interpretation_body(db, interp)


@router.post("/{connection_id}/external-transactions/{transaction_record_id}/classification")
def classify_provider_transaction(
    connection_id: int,
    transaction_record_id: int,
    payload: ProviderClassificationCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    expected_version: int | None = Header(default=None, alias="X-Expected-Version"),
):
    connection = get_owned_provider_connection(db, user_id=current_user.id, connection_id=connection_id)
    if connection is None:
        raise HTTPException(status_code=404, detail="Provider connection not found")
    _transaction_for_connection(db, user_id=current_user.id, connection_id=connection.id, transaction_record_id=transaction_record_id)
    expected = _expected_version(expected_version)
    key = _require_key(idempotency_key)
    request_payload = {"connection_id": connection_id, "transaction_record_id": transaction_record_id, "expected_version": expected, **payload.model_dump(mode="json")}
    request_hash = hash_command(request_payload)
    stored = _stored_or_conflict(db, user_id=current_user.id, command_type="CLASSIFY_EXTERNAL_TRANSACTION", key=key, request_hash=request_hash)
    if stored is not None:
        return stored.body
    try:
        interp = classify_external_transaction(
            db,
            user_id=current_user.id,
            transaction_record_id=transaction_record_id,
            expected_version=expected,
            event_type=payload.event_type,
            reason=payload.reason,
        )
    except StaleInterpretationVersionError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail={"code": "stale_version", "expected_version": exc.expected_version, "current_version": exc.current_version}) from exc
    except ProviderInterpretationError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    body = _interpretation_body(db, interp)
    add_command_receipt(db, user_id=current_user.id, command_type="CLASSIFY_EXTERNAL_TRANSACTION", idempotency_key=key, request_hash=request_hash, provider_interpretation_id=interp.id, response_status=200, response_body=body)
    raced = commit_with_idempotency_race_recovery(db, user_id=current_user.id, command_type="CLASSIFY_EXTERNAL_TRANSACTION", idempotency_key=key, request_hash=request_hash)
    return raced.body if raced is not None else body


@router.post("/{connection_id}/external-transactions/{transaction_record_id}/confirm")
def confirm_provider_transaction(
    connection_id: int,
    transaction_record_id: int,
    payload: ProviderInterpretationConfirm,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    expected_version: int | None = Header(default=None, alias="X-Expected-Version"),
):
    connection = get_owned_provider_connection(db, user_id=current_user.id, connection_id=connection_id)
    if connection is None:
        raise HTTPException(status_code=404, detail="Provider connection not found")
    _transaction_for_connection(db, user_id=current_user.id, connection_id=connection.id, transaction_record_id=transaction_record_id)
    expected = _expected_version(expected_version)
    key = _require_key(idempotency_key)
    request_payload = {"connection_id": connection_id, "transaction_record_id": transaction_record_id, "expected_version": expected, **payload.model_dump(mode="json")}
    request_hash = hash_command(request_payload)
    stored = _stored_or_conflict(db, user_id=current_user.id, command_type="CONFIRM_EXTERNAL_INTERPRETATION", key=key, request_hash=request_hash)
    if stored is not None:
        return stored.body
    try:
        interp = confirm_external_interpretation(
            db,
            user_id=current_user.id,
            transaction_record_id=transaction_record_id,
            expected_version=expected,
            event_type=payload.event_type,
            account_id=payload.account_id,
            actor_user_id=current_user.id,
            reason=payload.reason,
        )
    except StaleInterpretationVersionError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail={"code": "stale_version", "expected_version": exc.expected_version, "current_version": exc.current_version}) from exc
    except ProviderInterpretationError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    body = _interpretation_body(db, interp)
    add_command_receipt(db, user_id=current_user.id, command_type="CONFIRM_EXTERNAL_INTERPRETATION", idempotency_key=key, request_hash=request_hash, provider_interpretation_id=interp.id, response_status=200, response_body=body)
    raced = commit_with_idempotency_race_recovery(db, user_id=current_user.id, command_type="CONFIRM_EXTERNAL_INTERPRETATION", idempotency_key=key, request_hash=request_hash)
    return raced.body if raced is not None else body

