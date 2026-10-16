from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.authorization import Subject, owned
from app.models import AccountGrant, FinancialAccount

DEFAULT_CASH_ACCOUNT_NAME = "Default Cash"
DEFAULT_CURRENCY = "VND"


def create_default_cash_account(db: Session, user_id: int) -> FinancialAccount:
    account = FinancialAccount(
        user_id=user_id,
        name=DEFAULT_CASH_ACCOUNT_NAME,
        account_type="CASH",
        currency=DEFAULT_CURRENCY,
        is_default=True,
    )
    db.add(account)
    return account


def get_default_cash_account(db: Session, user_id: int) -> FinancialAccount | None:
    return (
        db.query(FinancialAccount)
        .filter(
            FinancialAccount.user_id == user_id,
            FinancialAccount.is_default.is_(True),
        )
        .first()
    )


def get_owned_account(
    db: Session,
    user_id: int,
    account_id: int,
) -> FinancialAccount | None:
    return (
        owned(db.query(FinancialAccount), FinancialAccount.user_id, Subject(user_id))
        .filter(FinancialAccount.id == account_id)
        .first()
    )


def get_readable_account(
    db: Session,
    user_id: int,
    account_id: int,
) -> FinancialAccount | None:
    shared_with_user = (
        db.query(AccountGrant.account_id)
        .filter(
            AccountGrant.account_id == FinancialAccount.id,
            AccountGrant.grantee_user_id == user_id,
            AccountGrant.role == "VIEWER",
        )
        .exists()
    )
    return (
        db.query(FinancialAccount)
        .filter(
            FinancialAccount.id == account_id,
            or_(
                FinancialAccount.user_id == user_id,
                shared_with_user,
            ),
        )
        .first()
    )
