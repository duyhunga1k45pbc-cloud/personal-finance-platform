from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.account_service import get_owned_account, get_readable_account
from app.authorization import Action, Authorized, ResourceType, Subject, authorize_object, owned
from app.database import SessionLocal
from app.models import AccountGrant, FinancialAccount, User
from app.routers.auth import Principal, authorize_owner_principal, get_principal
from app.schemas import (
    AccountGrantCreate,
    AccountGrantRead,
    FinancialAccountCreate,
    FinancialAccountRead,
)

router = APIRouter(prefix="/accounts", tags=["accounts"], dependencies=[Depends(authorize_owner_principal)])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.get("", response_model=list[FinancialAccountRead])
def list_accounts(
    db: Session = Depends(get_db),
    current_user: Principal = Depends(get_principal),
):
    return (
        owned(db.query(FinancialAccount), FinancialAccount.user_id, Subject(current_user.id))
        .order_by(FinancialAccount.id)
        .all()
    )


@router.post("", response_model=FinancialAccountRead)
def create_account(
    account: FinancialAccountCreate,
    db: Session = Depends(get_db),
    current_user: Principal = Depends(get_principal),
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


@router.get("/shared-with-me", response_model=list[FinancialAccountRead])
def list_shared_accounts(
    db: Session = Depends(get_db),
    current_user: Principal = Depends(get_principal),
):
    return (
        db.query(FinancialAccount)
        .join(AccountGrant, AccountGrant.account_id == FinancialAccount.id)
        .filter(
            AccountGrant.grantee_user_id == current_user.id,
            AccountGrant.role == "VIEWER",
        )
        .order_by(FinancialAccount.id)
        .all()
    )


@router.post(
    "/{account_id}/shares",
    response_model=AccountGrantRead,
    status_code=status.HTTP_201_CREATED,
)
def create_account_share(
    account_id: int,
    payload: AccountGrantCreate,
    db: Session = Depends(get_db),
    current_user: Principal = Depends(get_principal),
):
    account = get_owned_account(db, current_user.id, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    if payload.grantee_user_id == current_user.id:
        raise HTTPException(
            status_code=422,
            detail="Cannot share an account with yourself",
        )
    if db.query(User.id).filter(User.id == payload.grantee_user_id).first() is None:
        raise HTTPException(status_code=404, detail="User not found")

    grant = AccountGrant(
        account_id=account.id,
        grantee_user_id=payload.grantee_user_id,
        role="VIEWER",
    )
    db.add(grant)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        duplicate = (
            db.query(AccountGrant.account_id)
            .filter(
                AccountGrant.account_id == account.id,
                AccountGrant.grantee_user_id == payload.grantee_user_id,
            )
            .first()
        )
        if duplicate is not None:
            raise HTTPException(status_code=409, detail="Account is already shared")
        raise
    db.refresh(grant)
    return grant


@router.get(
    "/{account_id}/shares",
    response_model=list[AccountGrantRead],
)
def list_account_shares(
    account_id: int,
    db: Session = Depends(get_db),
    current_user: Principal = Depends(get_principal),
):
    account = get_owned_account(db, current_user.id, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    return (
        db.query(AccountGrant)
        .filter(AccountGrant.account_id == account.id)
        .order_by(AccountGrant.grantee_user_id)
        .all()
    )


@router.delete(
    "/{account_id}/shares/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def revoke_account_share(
    account_id: int,
    user_id: int,
    db: Session = Depends(get_db),
    current_user: Principal = Depends(get_principal),
):
    account = get_owned_account(db, current_user.id, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    grant = (
        db.query(AccountGrant)
        .filter(
            AccountGrant.account_id == account.id,
            AccountGrant.grantee_user_id == user_id,
        )
        .first()
    )
    if grant is None:
        raise HTTPException(status_code=404, detail="Account share not found")
    db.delete(grant)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{account_id}", response_model=FinancialAccountRead)
def get_account(
    account_id: int,
    db: Session = Depends(get_db),
    current_user: Principal = Depends(get_principal),
    authorized: Authorized = Depends(authorize_owner_principal),
):
    account = get_readable_account(db, current_user.id, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    if (
        authorized.policy.resource is not ResourceType.ACCOUNT
        or authorized.policy.action is not Action.READ
    ):
        raise HTTPException(status_code=403, detail="Forbidden")
    if account.user_id == current_user.id:
        authorize_object(authorized, ResourceType.ACCOUNT, Action.READ, account.user_id)
    return account
