from decimal import Decimal

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.account_service import get_owned_account
from app.command_service import (
    IdempotencyConflictError,
    add_command_receipt,
    commit_with_idempotency_race_recovery,
    get_stored_command_response,
    hash_command,
    normalize_idempotency_key,
)
from app.database import SessionLocal
from app.models import FinancialEventEntry, User
from app.routers.auth import get_current_user
from app.schemas import TransferCreate
from app.transfer_service import create_transfer_event

router = APIRouter(prefix="/transfers", tags=["transfers"])


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


def _owned_account_or_404(db: Session, user_id: int, account_id: int):
    account = get_owned_account(db, user_id, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    return account


def _transfer_response(db: Session, event) -> dict:
    entries = (
        db.query(FinancialEventEntry)
        .filter(FinancialEventEntry.financial_event_id == event.id)
        .order_by(FinancialEventEntry.id.asc())
        .all()
    )
    if len(entries) != 2:
        raise HTTPException(status_code=409, detail="Transfer invariant violated")

    negative = [entry for entry in entries if Decimal(entry.amount) < 0]
    positive = [entry for entry in entries if Decimal(entry.amount) > 0]
    if len(negative) != 1 or len(positive) != 1:
        raise HTTPException(status_code=409, detail="Transfer invariant violated")

    return {
        "id": event.id,
        "event_type": event.event_type,
        "amount": abs(Decimal(negative[0].amount)),
        "from_account_id": negative[0].account_id,
        "to_account_id": positive[0].account_id,
        "description": event.description,
        "occurred_at": event.occurred_at,
        "canonical_version": event.version,
    }


@router.post("")
def create_transfer(
    transfer: TransferCreate,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    command_type = "CREATE_TRANSFER"
    key = _normalize_command_key(idempotency_key)
    request_hash = hash_command(
        {
            "command_type": command_type,
            "payload": transfer.model_dump(mode="json"),
        }
    )

    try:
        stored = get_stored_command_response(
            db,
            user_id=current_user.id,
            command_type=command_type,
            idempotency_key=key,
            request_hash=request_hash,
        )
    except IdempotencyConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if stored is not None:
        return JSONResponse(status_code=stored.status_code, content=stored.body)

    if transfer.from_account_id == transfer.to_account_id:
        raise HTTPException(
            status_code=422,
            detail="Transfer source and destination accounts must be different",
        )

    from_account = _owned_account_or_404(
        db, current_user.id, transfer.from_account_id
    )
    to_account = _owned_account_or_404(
        db, current_user.id, transfer.to_account_id
    )

    event = create_transfer_event(
        db,
        user_id=current_user.id,
        from_account=from_account,
        to_account=to_account,
        amount=transfer.amount,
        description=transfer.description,
        occurred_at=transfer.occurred_at,
        actor_type="USER",
        actor_user_id=current_user.id,
        reason="create_transfer",
    )
    response_body = _transfer_response(db, event)
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

    try:
        race_replay = commit_with_idempotency_race_recovery(
            db,
            user_id=current_user.id,
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
