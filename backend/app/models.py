import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    text,
)

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


class FinancialEvent(Base):
    __tablename__ = "financial_events"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ('INCOME', 'EXPENSE', 'TRANSFER', 'REFUND', 'REVERSAL', 'ADJUSTMENT')",
            name="ck_financial_events_event_type",
        ),
        CheckConstraint(
            "interpretation_state IN ('UNCLASSIFIED', 'CLASSIFIED', 'USER_CONFIRMED')",
            name="ck_financial_events_interpretation_state",
        ),
        CheckConstraint(
            "provenance IN ('PROVIDER', 'USER_MANUAL', 'SYSTEM_INFERRED')",
            name="ck_financial_events_provenance",
        ),
        CheckConstraint(
            "confidence IN ('OBSERVED', 'INFERRED', 'USER_CONFIRMED')",
            name="ck_financial_events_confidence",
        ),
        CheckConstraint(
            "version >= 1",
            name="ck_financial_events_version_positive",
        ),
        UniqueConstraint(
            "legacy_transaction_id",
            name="uq_financial_events_legacy_transaction_id",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    # Transitional bridge during Task 2. The canonical model is not intended to
    # depend on legacy Transaction forever, so this is nullable for future events.
    legacy_transaction_id = Column(
        Integer,
        ForeignKey("transactions.id"),
        nullable=True,
        index=True,
    )

    event_type = Column(String, nullable=False)
    description = Column(String, nullable=True)
    category = Column(String, nullable=True)

    # Legacy Transaction.date has no timezone semantics. Task 2 preserves it
    # exactly rather than inventing timezone information during migration.
    occurred_at = Column(DateTime, nullable=False)
    effective_at = Column(DateTime, nullable=False)
    recorded_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
    )

    interpretation_state = Column(
        String,
        nullable=False,
        default="USER_CONFIRMED",
    )
    provenance = Column(String, nullable=False, default="USER_MANUAL")
    confidence = Column(String, nullable=False, default="USER_CONFIRMED")
    version = Column(Integer, nullable=False, default=1)


class FinancialEventEntry(Base):
    __tablename__ = "financial_event_entries"
    __table_args__ = (
        CheckConstraint(
            "amount <> 0",
            name="ck_financial_event_entries_nonzero_amount",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    financial_event_id = Column(
        Integer,
        ForeignKey("financial_events.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    account_id = Column(
        Integer,
        ForeignKey("financial_accounts.id"),
        nullable=False,
        index=True,
    )
    # Signed canonical movement for the account: + inflow, - outflow.
    amount = Column(Numeric(18, 2), nullable=False)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
    )
