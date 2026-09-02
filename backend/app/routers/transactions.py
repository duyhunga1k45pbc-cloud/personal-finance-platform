from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.account_service import get_default_cash_account, get_owned_account
from app.canonical_service import (
    ConcurrentModificationError,
    correct_legacy_transaction_with_expected_version,
    sync_canonical_from_legacy_transaction,
    void_canonical_with_expected_version,
)
from app.credit_card_service import validate_transaction_account_semantics
from app.command_service import (
    IdempotencyConflictError,
    add_command_receipt,
    commit_with_idempotency_race_recovery,
    get_stored_command_response,
    hash_command,
    normalize_idempotency_key,
)
from app.database import SessionLocal
from app.models import FinancialEvent, FinancialEventHistory, Transaction, User
from app.routers.auth import get_current_user
from app.schemas import TransactionCreate

router = APIRouter(prefix="/transactions", tags=["transactions"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _resolve_owned_account(
    db: Session,
    user_id: int,
    account_id: int | None,
):
    if account_id is None:
        account = get_default_cash_account(db, user_id)
        if account is None:
            raise HTTPException(
                status_code=409,
                detail="Default cash account is missing",
            )
        return account

    account = get_owned_account(db, user_id, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    return account


def _active_transaction_query(db: Session, user_id: int):
    return (
        db.query(Transaction)
        .join(
            FinancialEvent,
            FinancialEvent.legacy_transaction_id == Transaction.id,
        )
        .filter(
            Transaction.user_id == user_id,
            FinancialEvent.lifecycle_state == "ACTIVE",
        )
    )


def _normalize_command_key(raw_key: str) -> str:
    try:
        return normalize_idempotency_key(raw_key)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _stored_response_or_none(
    db: Session,
    *,
    user_id: int,
    command_type: str,
    idempotency_key: str,
    request_hash: str,
):
    try:
        stored = get_stored_command_response(
            db,
            user_id=user_id,
            command_type=command_type,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
    except IdempotencyConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    if stored is None:
        return None
    return JSONResponse(status_code=stored.status_code, content=stored.body)


def _transaction_response(transaction: Transaction, event: FinancialEvent) -> dict:
    return {
        "id": transaction.id,
        "amount": transaction.amount,
        "description": transaction.description,
        "category": transaction.category,
        "date": transaction.date,
        "type": transaction.type,
        "user_id": transaction.user_id,
        "account_id": transaction.account_id,
        "canonical_version": event.version,
    }


def _replay_after_concurrency_conflict(
    db: Session,
    *,
    user_id: int,
    command_type: str,
    idempotency_key: str,
    request_hash: str,
    exc: ConcurrentModificationError,
):
    db.rollback()
    replay = _stored_response_or_none(
        db,
        user_id=user_id,
        command_type=command_type,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
    )
    if replay is not None:
        return replay
    raise HTTPException(
        status_code=409,
        detail={
            "code": "stale_version",
            "expected_version": exc.expected_version,
            "current_version": exc.current_version,
        },
    ) from exc


@router.get("")
def list_transactions(
    type: Literal["income", "expense"] | None = Query(default=None),
    category: str | None = Query(default=None),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=10, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = _active_transaction_query(db, current_user.id)
    if type is not None:
        query = query.filter(Transaction.type == type)
    if category is not None:
        query = query.filter(Transaction.category == category)

    total = query.count()
    transactions = query.offset(skip).limit(limit).all()

    return {
        "total": total,
        "skip": skip,
        "limit": limit,
        "data": transactions,
    }


@router.get("/{transaction_id}")
def get_transaction(
    transaction_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    transaction = (
        _active_transaction_query(db, current_user.id)
        .filter(Transaction.id == transaction_id)
        .first()
    )

    if transaction is None:
        raise HTTPException(status_code=404, detail="Transaction not found")

    event = (
        db.query(FinancialEvent)
        .filter(FinancialEvent.legacy_transaction_id == transaction.id)
        .one()
    )
    return _transaction_response(transaction, event)


@router.get("/{transaction_id}/history")
def get_transaction_history(
    transaction_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    transaction = (
        db.query(Transaction)
        .filter(
            Transaction.id == transaction_id,
            Transaction.user_id == current_user.id,
        )
        .first()
    )
    if transaction is None:
        raise HTTPException(status_code=404, detail="Transaction not found")

    event = (
        db.query(FinancialEvent)
        .filter(FinancialEvent.legacy_transaction_id == transaction.id)
        .one_or_none()
    )
    if event is None:
        raise HTTPException(status_code=409, detail="Canonical event is missing")

    rows = (
        db.query(FinancialEventHistory)
        .filter(FinancialEventHistory.financial_event_id == event.id)
        .order_by(FinancialEventHistory.event_version.asc())
        .all()
    )
    return [
        {
            "id": row.id,
            "financial_event_id": row.financial_event_id,
            "event_version": row.event_version,
            "transition_type": row.transition_type,
            "actor_type": row.actor_type,
            "actor_user_id": row.actor_user_id,
            "previous_state": row.previous_state,
            "new_state": row.new_state,
            "reason": row.reason,
            "recorded_at": row.recorded_at,
        }
        for row in rows
    ]


@router.post("")
def create_transaction(
    transaction: TransactionCreate,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    command_type = "CREATE_TRANSACTION"
    key = _normalize_command_key(idempotency_key)
    request_hash = hash_command(
        {
            "command_type": command_type,
            "payload": transaction.model_dump(mode="json"),
        }
    )

    replay = _stored_response_or_none(
        db,
        user_id=current_user.id,
        command_type=command_type,
        idempotency_key=key,
        request_hash=request_hash,
    )
    if replay is not None:
        return replay

    account = _resolve_owned_account(
        db,
        current_user.id,
        transaction.account_id,
    )
    try:
        validate_transaction_account_semantics(account, transaction.type)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    new_item = Transaction(
        amount=transaction.amount,
        description=transaction.description,
        category=transaction.category,
        type=transaction.type,
        user_id=current_user.id,
        account_id=account.id,
    )

    db.add(new_item)
    db.flush()
    event = sync_canonical_from_legacy_transaction(
        db,
        new_item,
        actor_type="USER",
        actor_user_id=current_user.id,
        reason="legacy_api_create",
    )
    response_body = _transaction_response(new_item, event)
    add_command_receipt(
        db,
        user_id=current_user.id,
        command_type=command_type,
        idempotency_key=key,
        request_hash=request_hash,
        transaction_id=new_item.id,
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


@router.put("/{transaction_id}")
def update_transaction(
    transaction_id: int,
    transaction_update: TransactionCreate,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    expected_version: int = Header(..., alias="X-Expected-Version", ge=1),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    command_type = "UPDATE_TRANSACTION"
    key = _normalize_command_key(idempotency_key)
    request_hash = hash_command(
        {
            "command_type": command_type,
            "transaction_id": transaction_id,
            "expected_version": expected_version,
            "payload": transaction_update.model_dump(mode="json"),
        }
    )

    replay = _stored_response_or_none(
        db,
        user_id=current_user.id,
        command_type=command_type,
        idempotency_key=key,
        request_hash=request_hash,
    )
    if replay is not None:
        return replay

    transaction = (
        _active_transaction_query(db, current_user.id)
        .filter(Transaction.id == transaction_id)
        .first()
    )
    if transaction is None:
        raise HTTPException(status_code=404, detail="Transaction not found")

    requested_account_id = (
        transaction_update.account_id
        if transaction_update.account_id is not None
        else transaction.account_id
    )
    account = _resolve_owned_account(
        db,
        current_user.id,
        requested_account_id,
    )
    account_id = account.id
    try:
        validate_transaction_account_semantics(account, transaction_update.type)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        event = correct_legacy_transaction_with_expected_version(
            db,
            transaction,
            amount=transaction_update.amount,
            description=transaction_update.description,
            category=transaction_update.category,
            transaction_type=transaction_update.type,
            account_id=account_id,
            expected_version=expected_version,
            actor_type="USER",
            actor_user_id=current_user.id,
            reason="legacy_api_update",
        )
    except ConcurrentModificationError as exc:
        return _replay_after_concurrency_conflict(
            db,
            user_id=current_user.id,
            command_type=command_type,
            idempotency_key=key,
            request_hash=request_hash,
            exc=exc,
        )

    response_body = _transaction_response(transaction, event)
    add_command_receipt(
        db,
        user_id=current_user.id,
        command_type=command_type,
        idempotency_key=key,
        request_hash=request_hash,
        transaction_id=transaction.id,
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


@router.delete("/{transaction_id}")
def delete_transaction(
    transaction_id: int,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    expected_version: int = Header(..., alias="X-Expected-Version", ge=1),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    command_type = "DELETE_TRANSACTION"
    key = _normalize_command_key(idempotency_key)
    request_hash = hash_command(
        {
            "command_type": command_type,
            "transaction_id": transaction_id,
            "expected_version": expected_version,
        }
    )

    replay = _stored_response_or_none(
        db,
        user_id=current_user.id,
        command_type=command_type,
        idempotency_key=key,
        request_hash=request_hash,
    )
    if replay is not None:
        return replay

    transaction = (
        _active_transaction_query(db, current_user.id)
        .filter(Transaction.id == transaction_id)
        .first()
    )
    if transaction is None:
        raise HTTPException(status_code=404, detail="Transaction not found")

    try:
        event = void_canonical_with_expected_version(
            db,
            transaction_id,
            expected_version=expected_version,
            actor_type="USER",
            actor_user_id=current_user.id,
            reason="legacy_api_delete",
        )
    except ConcurrentModificationError as exc:
        return _replay_after_concurrency_conflict(
            db,
            user_id=current_user.id,
            command_type=command_type,
            idempotency_key=key,
            request_hash=request_hash,
            exc=exc,
        )

    response_body = {
        "message": "Transaction deleted successfully",
        "deleted_id": transaction_id,
        "canonical_version": event.version,
    }
    add_command_receipt(
        db,
        user_id=current_user.id,
        command_type=command_type,
        idempotency_key=key,
        request_hash=request_hash,
        transaction_id=transaction.id,
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
