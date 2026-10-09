from decimal import Decimal

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.causal_service import CausalEventError, create_refund_event, create_reversal_event
from app.command_service import (
    IdempotencyConflictError,
    add_command_receipt,
    commit_with_idempotency_race_recovery,
    get_stored_command_response,
    hash_command,
    normalize_idempotency_key,
)
from app.database import SessionLocal
from app.models import FinancialEventEntry, FinancialEventLink, User
from app.routers.auth import get_current_user
from app.schemas import RefundCreate, ReversalCreate

router = APIRouter(tags=["causal-events"])


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


def _replay_or_raise_causal_error(
    db: Session,
    *,
    user_id: int,
    command_type: str,
    key: str,
    request_hash: str,
    error: CausalEventError,
) -> JSONResponse:
    db.rollback()
    stored = _stored_or_none(
        db,
        user_id=user_id,
        command_type=command_type,
        key=key,
        request_hash=request_hash,
    )
    if stored is not None:
        return stored
    raise HTTPException(status_code=422, detail=str(error)) from error


def _causal_response(db: Session, event) -> dict:
    link = (
        db.query(FinancialEventLink)
        .filter(FinancialEventLink.from_event_id == event.id)
        .one()
    )
    entries = (
        db.query(FinancialEventEntry)
        .filter(FinancialEventEntry.financial_event_id == event.id)
        .order_by(FinancialEventEntry.id.asc())
        .all()
    )
    return {
        "id": event.id,
        "event_type": event.event_type,
        "original_event_id": link.to_event_id,
        "relation_type": link.relation_type,
        "entries": [
            {
                "account_id": entry.account_id,
                "amount": format(Decimal(entry.amount), "f"),
            }
            for entry in entries
        ],
        "description": event.description,
        "occurred_at": event.occurred_at,
        "canonical_version": event.version,
    }


@router.post("/refunds")
def create_refund(
    refund: RefundCreate,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    command_type = "CREATE_REFUND"
    key = _normalize_command_key(idempotency_key)
    request_hash = hash_command(
        {
            "command_type": command_type,
            "payload": refund.model_dump(mode="json"),
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
        event = create_refund_event(
            db,
            user_id=current_user.id,
            original_event_id=refund.original_event_id,
            amount=refund.amount,
            description=refund.description,
            occurred_at=refund.occurred_at,
            actor_type="USER",
            actor_user_id=current_user.id,
            reason="create_refund",
        )
    except CausalEventError as exc:
        return _replay_or_raise_causal_error(
            db,
            user_id=current_user.id,
            command_type=command_type,
            key=key,
            request_hash=request_hash,
            error=exc,
        )

    response_body = _causal_response(db, event)
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


@router.post("/reversals")
def create_reversal(
    reversal: ReversalCreate,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    command_type = "CREATE_REVERSAL"
    key = _normalize_command_key(idempotency_key)
    request_hash = hash_command(
        {
            "command_type": command_type,
            "payload": reversal.model_dump(mode="json"),
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
        event = create_reversal_event(
            db,
            user_id=current_user.id,
            original_event_id=reversal.original_event_id,
            description=reversal.description,
            occurred_at=reversal.occurred_at,
            actor_type="USER",
            actor_user_id=current_user.id,
            reason="create_reversal",
        )
    except CausalEventError as exc:
        return _replay_or_raise_causal_error(
            db,
            user_id=current_user.id,
            command_type=command_type,
            key=key,
            request_hash=request_hash,
            error=exc,
        )

    response_body = _causal_response(db, event)
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
