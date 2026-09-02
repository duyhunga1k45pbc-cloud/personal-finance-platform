from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.security import settings

SECRET_KEY = settings.secret_key
ALGORITHM = settings.jwt_algorithm
ACCESS_TOKEN_EXPIRE_MINUTES = settings.access_token_expire_minutes
JWT_ISSUER = settings.jwt_issuer
JWT_AUDIENCE = settings.jwt_audience

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Login deliberately performs a password verification even when an account does
# not exist, reducing the timing difference between "unknown email" and
# "wrong password". This hash is process-local and never represents a real user.
_DUMMY_PASSWORD_HASH = pwd_context.hash(
    "dummy-password-used-only-for-login-timing-equalization"
)


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def verify_password_or_dummy(
    plain_password: str,
    hashed_password: str | None,
) -> bool:
    return verify_password(
        plain_password,
        hashed_password or _DUMMY_PASSWORD_HASH,
    )


def create_access_token(data: dict) -> str:
    to_encode = data.copy()
    subject = str(to_encode.get("sub", "")).strip()
    if not subject:
        raise ValueError("Access token requires a non-empty subject.")

    now = datetime.now(timezone.utc)
    expire = now + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update(
        {
            "sub": subject,
            "type": "access",
            "iss": JWT_ISSUER,
            "aud": JWT_AUDIENCE,
            "iat": now,
            "nbf": now,
            "exp": expire,
            "jti": uuid4().hex,
        }
    )
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def decode_access_token(token: str):
    try:
        payload = jwt.decode(
            token,
            SECRET_KEY,
            algorithms=[ALGORITHM],
            audience=JWT_AUDIENCE,
            issuer=JWT_ISSUER,
            options={
                "verify_signature": True,
                "verify_aud": True,
                "verify_iss": True,
                "verify_exp": True,
                "verify_iat": True,
                "verify_nbf": True,
            },
        )
    except JWTError:
        return None

    if payload.get("type") != "access":
        return None

    subject = str(payload.get("sub", "")).strip()
    if not subject:
        return None

    return payload
