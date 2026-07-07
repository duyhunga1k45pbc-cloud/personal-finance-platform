from fastapi import FastAPI, Depends
from sqlalchemy.orm import Session

from app.database import Base, engine, SessionLocal
from app.models import Transaction
from app.routers import transactions

Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="Personal Finance API",
    description="Backend API quản lý thu chi cá nhân",
    version="1.0.0"
)

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
def get_summary(db: Session = Depends(get_db)):
    transactions = db.query(Transaction).all()

    total_income = 0
    total_expense = 0

    for transaction in transactions:
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