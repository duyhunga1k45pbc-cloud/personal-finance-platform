from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class TransactionCreate(BaseModel):
    amount: Decimal = Field(gt=0, max_digits=18, decimal_places=2)
    description: str
    category: str
    type: Literal["income", "expense"]
    account_id: int | None = None


class FinancialAccountCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    account_type: Literal["BANK", "EWALLET", "CREDIT_CARD", "CASH"]
    currency: Literal["VND"] = "VND"


class FinancialAccountRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    name: str
    account_type: str
    currency: str
    is_default: bool
    version: int


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6, max_length=72)


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class Token(BaseModel):
    access_token: str
    token_type: str


class TransferCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: Decimal = Field(gt=0, max_digits=18, decimal_places=2)
    from_account_id: int
    to_account_id: int
    description: str | None = Field(default=None, max_length=255)
    occurred_at: datetime | None = None


class RefundCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    original_event_id: int
    amount: Decimal = Field(gt=0, max_digits=18, decimal_places=2)
    description: str | None = Field(default=None, max_length=255)
    occurred_at: datetime | None = None


class ReversalCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    original_event_id: int
    description: str | None = Field(default=None, max_length=255)
    occurred_at: datetime | None = None


class ReconciliationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: int
    observed_balance: Decimal = Field(ge=0, max_digits=18, decimal_places=2)
    observed_at: datetime | None = None
    note: str | None = Field(default=None, max_length=255)


class ReconciliationResolve(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=255)


class AdjustmentConfirm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm: Literal[True]
    reason: str = Field(min_length=3, max_length=255)

class ProviderConnectionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_name: str = Field(min_length=1, max_length=100)
    external_account_id: str = Field(min_length=1, max_length=255)
    display_name: str | None = Field(default=None, max_length=255)


class ExternalTransactionEvidenceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    external_transaction_id: str = Field(min_length=1, max_length=255)
    observed_at: datetime | None = None
    raw_payload: dict[str, Any]

class ProviderNormalizationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_id: int
    normalizer_version: str = Field(min_length=1, max_length=64)
    amount: Decimal = Field(gt=0, max_digits=18, decimal_places=2)
    currency: str = Field(min_length=3, max_length=3)
    direction: Literal["INFLOW", "OUTFLOW"]
    normalized_status: Literal["PENDING", "POSTED", "REVERSED", "UNKNOWN"]
    occurred_at: datetime
    description: str | None = Field(default=None, max_length=255)
    provider_status: str | None = Field(default=None, max_length=64)


class ProviderClassificationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_type: Literal["INCOME", "EXPENSE", "TRANSFER", "REFUND", "REVERSAL"]
    reason: str | None = Field(default=None, max_length=255)


class ProviderInterpretationConfirm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_type: Literal["INCOME", "EXPENSE", "TRANSFER", "REFUND", "REVERSAL"]
    account_id: int
    reason: str | None = Field(default=None, max_length=255)

