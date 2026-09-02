from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.account_service import get_owned_account
from app.database import SessionLocal
from app.models import FinancialAccount, User
from app.routers.auth import get_current_user
from app.schemas import FinancialAccountCreate, FinancialAccountRead

router = APIRouter(prefix="/accounts", tags=["accounts"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.get("", response_model=list[FinancialAccountRead])
def list_accounts(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return (
        db.query(FinancialAccount)
        .filter(FinancialAccount.user_id == current_user.id)
        .order_by(FinancialAccount.id)
        .all()
    )


@router.post("", response_model=FinancialAccountRead)
def create_account(
    account: FinancialAccountCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    new_account = FinancialAccount(
        user_id=current_user.id,
        name=account.name,
        account_type=account.account_type,
        currency=account.currency,
        is_default=False,
    )
    db.add(new_account)
    db.commit()
    db.refresh(new_account)
    return new_account


@router.get("/{account_id}", response_model=FinancialAccountRead)
def get_account(
    account_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    account = get_owned_account(db, current_user.id, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    return account
