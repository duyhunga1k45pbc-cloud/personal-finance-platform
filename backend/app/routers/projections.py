from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import FinancialAccountBalanceProjection, User
from app.projection_service import (
    ProjectionMissingError,
    ProjectionStaleError,
    projection_status,
    rebuild_user_projection,
    require_fresh_projection,
)
from app.routers.auth import get_current_user


router = APIRouter(prefix="/projections", tags=["projections"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _state_payload(state):
    return {
        "generation": state.generation,
        "canonical_fingerprint": state.canonical_fingerprint,
        "total_income": state.total_income,
        "total_expense": state.total_expense,
        "balance": state.economic_balance,
        "net_worth": state.net_worth,
        "account_count": state.account_count,
        "rebuilt_at": state.rebuilt_at,
    }


def _fresh_state_or_http_error(db: Session, user_id: int):
    try:
        return require_fresh_projection(db, user_id)
    except ProjectionMissingError as exc:
        raise HTTPException(status_code=409, detail="projection_missing") from exc
    except ProjectionStaleError as exc:
        raise HTTPException(status_code=409, detail="projection_stale") from exc


@router.post("/rebuild")
def rebuild_projection(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    state = rebuild_user_projection(db, current_user.id)
    return _state_payload(state)


@router.get("/status")
def get_projection_status(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    status = projection_status(db, current_user.id)
    return {
        "status": status.status,
        "generation": status.generation,
        "stored_fingerprint": status.stored_fingerprint,
        "current_fingerprint": status.current_fingerprint,
        "rebuilt_at": status.rebuilt_at,
    }


@router.get("/summary")
def get_projection_summary(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return _state_payload(_fresh_state_or_http_error(db, current_user.id))


@router.get("/accounts")
def get_projected_account_balances(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    state = _fresh_state_or_http_error(db, current_user.id)
    rows = (
        db.query(FinancialAccountBalanceProjection)
        .filter(
            FinancialAccountBalanceProjection.user_id == current_user.id,
            FinancialAccountBalanceProjection.generation == state.generation,
        )
        .order_by(FinancialAccountBalanceProjection.account_id.asc())
        .all()
    )
    return [
        {
            "account_id": row.account_id,
            "generation": row.generation,
            "balance": row.balance,
            "rebuilt_at": row.rebuilt_at,
        }
        for row in rows
    ]
