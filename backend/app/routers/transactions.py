from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.account_service import get_default_cash_account, get_owned_account
from app.canonical_service import (
    sync_canonical_from_legacy_transaction,
    void_canonical_for_legacy_transaction,
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

    return transaction


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
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    account = _resolve_owned_account(
        db,
        current_user.id,
        transaction.account_id,
    )

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
    sync_canonical_from_legacy_transaction(
        db,
        new_item,
        actor_type="USER",
        actor_user_id=current_user.id,
        reason="legacy_api_create",
    )
    db.commit()
    db.refresh(new_item)

    return new_item


@router.put("/{transaction_id}")
def update_transaction(
    transaction_id: int,
    transaction_update: TransactionCreate,
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

    if transaction_update.account_id is not None:
        account = _resolve_owned_account(
            db,
            current_user.id,
            transaction_update.account_id,
        )
        transaction.account_id = account.id

    transaction.amount = transaction_update.amount
    transaction.description = transaction_update.description
    transaction.category = transaction_update.category
    transaction.type = transaction_update.type

    sync_canonical_from_legacy_transaction(
        db,
        transaction,
        actor_type="USER",
        actor_user_id=current_user.id,
        reason="legacy_api_update",
    )
    db.commit()
    db.refresh(transaction)

    return transaction


@router.delete("/{transaction_id}")
def delete_transaction(
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

    # Task 3 correction semantics: preserve the legacy row and canonical event,
    # but void the canonical event so it stops contributing to current state.
    void_canonical_for_legacy_transaction(
        db,
        transaction_id,
        actor_type="USER",
        actor_user_id=current_user.id,
        reason="legacy_api_delete",
    )
    db.commit()
    return {
        "message": "Transaction deleted successfully",
        "deleted_id": transaction_id,
    }
