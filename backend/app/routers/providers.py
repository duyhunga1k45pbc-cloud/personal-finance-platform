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
from app.models import ExternalTransaction, ExternalTransactionEvidence, ProviderConnection, User
from app.provider_service import (
    get_or_create_provider_connection,
    get_owned_provider_connection,
    ingest_external_evidence,
)
from app.routers.auth import get_current_user
from app.schemas import ExternalTransactionEvidenceCreate, ProviderConnectionCreate

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
