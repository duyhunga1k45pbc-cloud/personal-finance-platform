from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.account_service import create_default_cash_account
from app.auth import (
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password_or_dummy,
)
from app.database import SessionLocal
from app.models import User
from app.schemas import Token, UserCreate, UserLogin

router = APIRouter(prefix="/auth", tags=["auth"])
security = HTTPBearer(auto_error=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail="Invalid authentication credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _normalize_email(email: str) -> str:
    return email.strip().lower()


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    db: Session = Depends(get_db),
):
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _unauthorized()

    payload = decode_access_token(credentials.credentials)
    if payload is None:
        raise _unauthorized()

    try:
        user_id = int(payload["sub"])
    except (KeyError, TypeError, ValueError):
        raise _unauthorized()

    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        # A token for a deleted/nonexistent principal is not an authorization
        # resource lookup failure; it is invalid authentication.
        raise _unauthorized()

    return user


@router.post("/register")
def register(user: UserCreate, db: Session = Depends(get_db)):
    email = _normalize_email(str(user.email))
    existing_user = (
        db.query(User)
        .filter(func.lower(User.email) == email)
        .first()
    )
    if existing_user:
        raise HTTPException(status_code=409, detail="Email already registered")

    new_user = User(
        email=email,
        hashed_password=hash_password(user.password),
    )

    try:
        # User creation and default account creation are one state transition.
        db.add(new_user)
        db.flush()
        create_default_cash_account(db, new_user.id)
        db.commit()
    except IntegrityError:
        # Concurrent registration for the same normalized identity must not
        # escape as a 500 or leave a partial account state.
        db.rollback()
        raise HTTPException(status_code=409, detail="Email already registered")

    db.refresh(new_user)
    return {
        "id": new_user.id,
        "email": new_user.email,
        "message": "User registered successfully",
    }


@router.post("/login", response_model=Token)
def login(user: UserLogin, db: Session = Depends(get_db)):
    email = _normalize_email(str(user.email))
    db_user = (
        db.query(User)
        .filter(func.lower(User.email) == email)
        .first()
    )

    password_valid = verify_password_or_dummy(
        user.password,
        db_user.hashed_password if db_user is not None else None,
    )
    if db_user is None or not password_valid:
        raise _unauthorized()

    access_token = create_access_token(
        data={
            "sub": str(db_user.id),
            "email": db_user.email,
        }
    )
    return {"access_token": access_token, "token_type": "bearer"}


@router.get("/me")
def get_me(current_user: User = Depends(get_current_user)):
    return {"id": current_user.id, "email": current_user.email}
