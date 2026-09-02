import datetime
from sqlalchemy import Boolean, CheckConstraint, Column, Integer, String, DateTime, ForeignKey, Index, Numeric, text
from app.database import Base


class Transaction(Base):
    __tablename__ = "transactions"
    id = Column(Integer, primary_key=True, index=True)
    amount = Column(Numeric(18, 2), nullable=False)
    description = Column(String)
    category = Column(String)
    date = Column(DateTime, default=datetime.datetime.utcnow)
    type = Column(String, nullable=False, default="expense")
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    account_id = Column(
        Integer,
        ForeignKey("financial_accounts.id"),
        nullable=False,
        index=True,
    )


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class FinancialAccount(Base):
    __tablename__ = "financial_accounts"
    __table_args__ = (
        CheckConstraint(
            "account_type IN ('BANK', 'EWALLET', 'CREDIT_CARD', 'CASH')",
            name="ck_financial_accounts_account_type",
        ),
        CheckConstraint(
            "currency = 'VND'",
            name="ck_financial_accounts_currency_vnd",
        ),
        CheckConstraint(
            "version >= 1",
            name="ck_financial_accounts_version_positive",
        ),
        CheckConstraint(
            "NOT is_default OR account_type = 'CASH'",
            name="ck_financial_accounts_default_is_cash",
        ),
        Index(
            "uq_financial_accounts_default_per_user",
            "user_id",
            unique=True,
            postgresql_where=text("is_default"),
            sqlite_where=text("is_default = 1"),
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String, nullable=False)
    account_type = Column(String, nullable=False)
    currency = Column(String(3), nullable=False, default="VND")
    is_default = Column(Boolean, nullable=False, default=False)
    version = Column(Integer, nullable=False, default=1)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
        onupdate=lambda: datetime.datetime.now(datetime.timezone.utc),
    )
