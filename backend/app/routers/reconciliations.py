from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import JSONResponse
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
from app.models import ReconciliationCase, User
from app.reconciliation_service import (
    ReconciliationConflictError,
    ReconciliationError,
    ReconciliationNotFoundError,
    confirm_reconciliation_adjustment,
    detect_cash_reconciliation,
    get_owned_reconciliation,
    resolve_reconciliation_with_real_events,
)
from app.routers.auth import get_current_user
from app.schemas import AdjustmentConfirm, ReconciliationCreate, ReconciliationResolve

router = APIRouter(prefix="/reconciliations", tags=["reconciliations"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _normalize_command_key(raw_key: str) -> str:
    try:
        return normalize_idempotency_key(raw_key)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _stored_or_none(
    db: Session,
    *,
    user_id: int,
    command_type: str,
    key: str,
    request_hash: str,
):
    try:
        stored = get_stored_command_response(
            db,
            user_id=user_id,
            command_type=command_type,
            idempotency_key=key,
            request_hash=request_hash,
        )
    except IdempotencyConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if stored is None:
        return None
    return JSONResponse(status_code=stored.status_code, content=stored.body)


def _commit_or_replay(
    db: Session,
    *,
    user_id: int,
    command_type: str,
    key: str,
    request_hash: str,
    response_body: dict,
):
    try:
        race_replay = commit_with_idempotency_race_recovery(
            db,
            user_id=user_id,
            command_type=command_type,
            idempotency_key=key,
            request_hash=request_hash,
        )
    except IdempotencyConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if race_replay is not None:
        return JSONResponse(
            status_code=race_replay.status_code,
            content=race_replay.body,
        )
    return response_body


def _case_response(case: ReconciliationCase) -> dict:
    return {
        "id": case.id,
        "account_id": case.account_id,
        "expected_balance": format(case.expected_balance, "f"),
        "observed_balance": format(case.observed_balance, "f"),
        "difference": format(case.difference, "f"),
        "status": case.status,
        "resolution_type": case.resolution_type,
        "adjustment_event_id": case.adjustment_event_id,
        "resolved_balance": (
            format(case.resolved_balance, "f")
            if case.resolved_balance is not None
            else None
        ),
        "note": case.note,
        "version": case.version,
        "observed_at": case.observed_at.isoformat() if case.observed_at else None,
        "resolved_at": case.resolved_at.isoformat() if case.resolved_at else None,
    }


def _translate_reconciliation_error(exc: ReconciliationError):
    if isinstance(exc, ReconciliationNotFoundError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, ReconciliationConflictError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/{reconciliation_id}")
def get_reconciliation(
    reconciliation_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        case = get_owned_reconciliation(
            db,
            user_id=current_user.id,
            reconciliation_id=reconciliation_id,
        )
    except ReconciliationError as exc:
        _translate_reconciliation_error(exc)
    return _case_response(case)


@router.post("")
def create_reconciliation(
    payload: ReconciliationCreate,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    command_type = "CREATE_RECONCILIATION"
    key = _normalize_command_key(idempotency_key)
    request_hash = hash_command(
        {
            "command_type": command_type,
            "payload": payload.model_dump(mode="json"),
        }
    )
    stored = _stored_or_none(
        db,
        user_id=current_user.id,
        command_type=command_type,
        key=key,
        request_hash=request_hash,
    )
    if stored is not None:
        return stored

    try:
        case = detect_cash_reconciliation(
            db,
            user_id=current_user.id,
            account_id=payload.account_id,
            observed_balance=payload.observed_balance,
            observed_at=payload.observed_at,
            note=payload.note,
            actor_user_id=current_user.id,
        )
    except ReconciliationError as exc:
        db.rollback()
        _translate_reconciliation_error(exc)

    response_body = _case_response(case)
    add_command_receipt(
        db,
        user_id=current_user.id,
        command_type=command_type,
        idempotency_key=key,
        request_hash=request_hash,
        reconciliation_id=case.id,
        response_status=200,
        response_body=response_body,
    )
    return _commit_or_replay(
        db,
        user_id=current_user.id,
        command_type=command_type,
        key=key,
        request_hash=request_hash,
        response_body=response_body,
    )


@router.post("/{reconciliation_id}/resolve")
def resolve_reconciliation(
    reconciliation_id: int,
    payload: ReconciliationResolve,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    expected_version: int = Header(..., alias="X-Expected-Version"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    command_type = "RESOLVE_RECONCILIATION"
    key = _normalize_command_key(idempotency_key)
    request_hash = hash_command(
        {
            "command_type": command_type,
            "reconciliation_id": reconciliation_id,
            "expected_version": expected_version,
            "payload": payload.model_dump(mode="json"),
        }
    )
    stored = _stored_or_none(
        db,
        user_id=current_user.id,
        command_type=command_type,
        key=key,
        request_hash=request_hash,
    )
    if stored is not None:
        return stored

    try:
        case = resolve_reconciliation_with_real_events(
            db,
            user_id=current_user.id,
            reconciliation_id=reconciliation_id,
            expected_version=expected_version,
            reason=payload.reason,
            actor_user_id=current_user.id,
        )
    except ReconciliationError as exc:
        db.rollback()
        _translate_reconciliation_error(exc)

    response_body = _case_response(case)
    add_command_receipt(
        db,
        user_id=current_user.id,
        command_type=command_type,
        idempotency_key=key,
        request_hash=request_hash,
        reconciliation_id=case.id,
        response_status=200,
        response_body=response_body,
    )
    return _commit_or_replay(
        db,
        user_id=current_user.id,
        command_type=command_type,
        key=key,
        request_hash=request_hash,
        response_body=response_body,
    )


@router.post("/{reconciliation_id}/adjustments")
def confirm_adjustment(
    reconciliation_id: int,
    payload: AdjustmentConfirm,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    expected_version: int = Header(..., alias="X-Expected-Version"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    command_type = "CONFIRM_RECONCILIATION_ADJUSTMENT"
    key = _normalize_command_key(idempotency_key)
    request_hash = hash_command(
        {
            "command_type": command_type,
            "reconciliation_id": reconciliation_id,
            "expected_version": expected_version,
            "payload": payload.model_dump(mode="json"),
        }
    )
    stored = _stored_or_none(
        db,
        user_id=current_user.id,
        command_type=command_type,
        key=key,
        request_hash=request_hash,
    )
    if stored is not None:
        return stored

    try:
        case, event = confirm_reconciliation_adjustment(
            db,
            user_id=current_user.id,
            reconciliation_id=reconciliation_id,
            expected_version=expected_version,
            reason=payload.reason,
            actor_user_id=current_user.id,
        )
    except ReconciliationError as exc:
        db.rollback()
        _translate_reconciliation_error(exc)

    response_body = {
        **_case_response(case),
        "adjustment_event_id": event.id,
        "adjustment_event_type": event.event_type,
        "adjustment_amount": format(case.difference, "f"),
    }
    add_command_receipt(
        db,
        user_id=current_user.id,
        command_type=command_type,
        idempotency_key=key,
        request_hash=request_hash,
        financial_event_id=event.id,
        response_status=200,
        response_body=response_body,
    )
    return _commit_or_replay(
        db,
        user_id=current_user.id,
        command_type=command_type,
        key=key,
        request_hash=request_hash,
        response_body=response_body,
    )
