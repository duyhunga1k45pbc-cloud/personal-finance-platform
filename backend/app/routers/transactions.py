from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import Transaction, User
from app.schemas import TransactionCreate
from app.routers.auth import get_current_user

router = APIRouter(
    prefix="/transactions",
    tags=["transactions"]
)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.get("")
def list_transactions(
    type: Literal["income", "expense"] | None = Query(default=None),
    category: str | None = Query(default=None),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=10, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    query = db.query(Transaction).filter(Transaction.user_id == current_user.id)
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
        "data": transactions
    }


@router.get("/{transaction_id}")
def get_transaction(transaction_id: int, db: Session = Depends(get_db),current_user: User = Depends(get_current_user)):
    transaction = db.query(Transaction).filter(
    Transaction.id == transaction_id,
    Transaction.user_id == current_user.id
).first()

    if transaction is None:
        raise HTTPException(status_code=404, detail="Transaction not found")

    return transaction


@router.post("")
def create_transaction(
    transaction: TransactionCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    new_item = Transaction(
        amount=transaction.amount,
        description=transaction.description,
        category=transaction.category,
        type=transaction.type,
        user_id=current_user.id
    )

    db.add(new_item)
    db.commit()
    db.refresh(new_item)

    return new_item


@router.put("/{transaction_id}")
def update_transaction(
    transaction_id: int,
    transaction_update: TransactionCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    transaction = db.query(Transaction).filter(
    Transaction.id == transaction_id,
    Transaction.user_id == current_user.id
).first()
    if transaction is None:
        raise HTTPException(status_code=404, detail="Transaction not found")

    transaction.amount = transaction_update.amount
    transaction.description = transaction_update.description
    transaction.category = transaction_update.category
    transaction.type = transaction_update.type

    db.commit()
    db.refresh(transaction)

    return transaction


@router.delete("/{transaction_id}")
def delete_transaction(transaction_id: int, db: Session = Depends(get_db),current_user: User = Depends(get_current_user)):
    transaction = db.query(Transaction).filter(
    Transaction.id == transaction_id,
    Transaction.user_id == current_user.id
).first()

    if transaction is None:
        raise HTTPException(status_code=404, detail="Transaction not found")

    db.delete(transaction)
    db.commit()

    return {
        "message": "Transaction deleted successfully",
        "deleted_id": transaction_id
    }


