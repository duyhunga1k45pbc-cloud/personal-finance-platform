from fastapi import Depends, FastAPI
from sqlalchemy.orm import Session

from app import models
from app.canonical_service import canonical_summary
from app.database import SessionLocal
from app.models import User
from app.routers import accounts, auth, causal_events, projections, providers, reconciliations, transactions, transfers
from app.routers.auth import get_current_user

app = FastAPI(
    title="Personal Finance API",
    description="Backend API quản lý thu chi cá nhân",
    version="1.0.0",
)

app.include_router(auth.router)
app.include_router(accounts.router)
app.include_router(transactions.router)
app.include_router(transfers.router)
app.include_router(causal_events.router)
app.include_router(reconciliations.router)
app.include_router(projections.router)
app.include_router(providers.router)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@app.get("/")
def home():
    return {"message": "Chào mừng bạn đến với ứng dụng Quản lý Tài chính!"}


@app.get("/summary")
def get_summary(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    summary = canonical_summary(db, current_user.id)
    return {
        "total_income": summary.total_income,
        "total_expense": summary.total_expense,
        "balance": summary.balance,
    }
