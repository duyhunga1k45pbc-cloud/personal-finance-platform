
from typing import Literal
from pydantic import BaseModel, Field, EmailStr


class TransactionCreate(BaseModel):
    amount: float = Field(gt=0)
    description: str
    category: str
    type: Literal["income", "expense"]


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6, max_length=72)


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class Token(BaseModel):
    access_token: str
    token_type: str