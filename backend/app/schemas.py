from datetime import datetime
from decimal import Decimal
from typing import Literal

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
