from fastapi import FastAPI, Depends
from sqlalchemy.orm import Session
from app import models

from app.database import Base, engine, SessionLocal

from app.routers import transactions, auth
from app.models import Transaction, User
from app.routers.auth import get_current_user

Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="Personal Finance API",
    description="Backend API quản lý thu chi cá nhân",
    version="1.0.0"
)

app.include_router(auth.router)

app.include_router(transactions.router)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@app.get("/")
def home():
    return {
        "message": "Chào mừng bạn đến với ứng dụng Quản lý Tài chính!"
    }


@app.get("/summary")
def get_summary(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    transactions_list = db.query(Transaction).filter(
        Transaction.user_id == current_user.id
    ).all()

    total_income = 0
    total_expense = 0

    for transaction in transactions_list:
        if transaction.type == "income":
            total_income += transaction.amount
        elif transaction.type == "expense":
            total_expense += transaction.amount

    balance = total_income - total_expense

    return {
        "total_income": total_income,
        "total_expense": total_expense,
        "balance": balance
    }