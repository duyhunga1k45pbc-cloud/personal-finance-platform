from fastapi import Depends, FastAPI, Request
from sqlalchemy.orm import Session

from app import models
from app.canonical_service import canonical_summary
from app.database import SessionLocal
from app.models import User
from app.routers import accounts, auth, causal_events, projections, providers, reconciliations, transactions, transfers
from app.routers.auth import get_current_user
from app.security import settings

app = FastAPI(
    title="Personal Finance API",
    description="Backend API quản lý thu chi cá nhân",
    version="1.0.0",
    docs_url=None if settings.is_production else "/docs",
    redoc_url=None if settings.is_production else "/redoc",
    openapi_url=None if settings.is_production else "/openapi.json",
)


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    # Financial responses and bearer-token responses should not be cached by
    # browsers or intermediary proxies unless a future endpoint opts in.
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return response

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
