"""Offline OIDC security checks for Personal Finance Platform.

Run from backend/: python -m pytest -q tests/test_oidc_security.py
No live Keycloak, real token, or production database is required.
"""

from __future__ import annotations

import base64
import time

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from jose import jwt
from sqlalchemy import Column, Integer, String, create_engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker
from sqlalchemy.pool import StaticPool

from app import oidc
from app.auth import create_access_token, decode_access_token
from app.authorization import Subject, owned
from app.database import Base
from app.models import ExternalIdentity, User
from app.routers import auth as auth_router

ISSUER = "http://localhost:8081/realms/pfp-dev"
AUDIENCE = "pfp-api"
CLIENT_ID = "moneta"
SUBJECT = "fbf7c744-6ebe-4fa7-9f6a-feb31e765f26"


def _b64_integer(value: int) -> str:
    binary = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(binary).rstrip(b"=").decode("ascii")


@pytest.fixture(scope="module")
def signing_material():
    """Test-only RSA pair; private key never leaves the test process."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public = key.public_key().public_numbers()
    jwk = {
        "kty": "RSA",
        "kid": "test-rs256-key-1",
        "alg": "RS256",
        "use": "sig",
        "n": _b64_integer(public.n),
        "e": _b64_integer(public.e),
    }
    return pem, jwk


@pytest.fixture
def mint_token(monkeypatch, signing_material):
    private_pem, public_jwk = signing_material
    monkeypatch.setattr(oidc, "OIDC_ISSUER", ISSUER)
    monkeypatch.setattr(oidc, "OIDC_AUDIENCE", AUDIENCE)
    monkeypatch.setattr(oidc, "OIDC_CLIENT_ID", CLIENT_ID)
    monkeypatch.setattr(oidc, "_get_jwks", lambda refresh=False: [public_jwk])

    def mint(**overrides):
        now = int(time.time())
        claims = {
            "iss": ISSUER,
            "aud": [AUDIENCE, "account"],
            "sub": SUBJECT,
            "azp": CLIENT_ID,
            "typ": "Bearer",
            "iat": now,
            "nbf": now,
            "exp": now + 300,
        }
        claims.update(overrides)
        return jwt.encode(
            claims,
            private_pem,
            algorithm="RS256",
            headers={"kid": public_jwk["kid"]},
        )

    return mint


def _bearer(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


@pytest.fixture
def demo_db():
    """Isolated in-memory DB containing only auth-related tables."""
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine,
        tables=[User.__table__, ExternalIdentity.__table__],
    )
    factory = sessionmaker(bind=engine)
    with factory() as db:
        db.add(User(id=1, email="pfp-oidc-demo@example.com", hashed_password="test-only"))
        db.add(User(id=2, email="another-user@example.com", hashed_password="test-only"))
        db.commit()
    yield factory
    engine.dispose()


def _map_identity(factory, *, issuer=ISSUER, subject=SUBJECT, user_id=1):
    with factory() as db:
        db.add(ExternalIdentity(issuer=issuer, subject=subject, user_id=user_id))
        db.commit()


def test_valid_keycloak_access_token(mint_token):
    assert oidc.verify_oidc_access_token(mint_token()) == {
        "issuer": ISSUER,
        "subject": SUBJECT,
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"iss": "http://localhost:8081/realms/other"},
        {"aud": ["another-api"]},
        {"azp": "unexpected-client"},
        {"typ": "ID"},  # A signed ID token must not authorize API access.
        {"sub": ""},
    ],
    ids=["wrong-issuer", "wrong-audience", "wrong-client", "id-token", "empty-sub"],
)
def test_validly_signed_but_wrong_claims_are_rejected(mint_token, changes):
    assert oidc.verify_oidc_access_token(mint_token(**changes)) is None


def test_expired_token_is_rejected(mint_token):
    now = int(time.time())
    assert oidc.verify_oidc_access_token(mint_token(iat=now - 600, exp=now - 300)) is None


def test_tampered_signature_is_rejected(mint_token):
    original = mint_token()
    header, claims, signature = original.split(".")
    bad_first = "A" if signature[0] != "A" else "B"
    tampered = ".".join((header, claims, bad_first + signature[1:]))
    assert oidc.verify_oidc_access_token(original) is not None
    assert oidc.verify_oidc_access_token(tampered) is None


def test_local_hs256_jwt_does_not_pass_as_oidc_token(mint_token):
    assert oidc.verify_oidc_access_token(create_access_token({"sub": "1"})) is None
    assert decode_access_token(create_access_token({"sub": "1"})) is not None


def test_unknown_jwks_key_is_rejected(mint_token, monkeypatch):
    monkeypatch.setattr(oidc, "_get_jwks", lambda refresh=False: [])
    assert oidc.verify_oidc_access_token(mint_token()) is None


def test_jwks_rotation_refreshes_unknown_kid(mint_token, signing_material, monkeypatch):
    public_jwk = signing_material[1]
    seen = []

    def keys(refresh=False):
        seen.append(refresh)
        return [public_jwk] if refresh else []

    monkeypatch.setattr(oidc, "_get_jwks", keys)
    assert oidc.verify_oidc_access_token(mint_token()) is not None
    assert seen == [False, True]


def test_jwks_network_failure_is_fail_closed(mint_token, monkeypatch):
    def unavailable(refresh=False):
        raise OSError("JWKS endpoint unavailable")

    monkeypatch.setattr(oidc, "_get_jwks", unavailable)
    assert oidc.verify_oidc_access_token(mint_token()) is None


def test_unmapped_identity_cannot_become_local_principal(mint_token, demo_db, monkeypatch):
    monkeypatch.setattr(auth_router, "SessionLocal", demo_db)
    with pytest.raises(HTTPException) as error:
        auth_router.get_principal(credentials=_bearer(mint_token()))
    assert error.value.status_code == 401


def test_mapped_identity_becomes_expected_local_principal(mint_token, demo_db, monkeypatch):
    _map_identity(demo_db)
    monkeypatch.setattr(auth_router, "SessionLocal", demo_db)
    principal = auth_router.get_principal(credentials=_bearer(mint_token()))
    assert principal.id == 1
    assert principal.email == "pfp-oidc-demo@example.com"


def test_identity_mapping_is_bound_to_exact_issuer_and_subject(mint_token, demo_db, monkeypatch):
    _map_identity(demo_db, subject="somebody-else")
    monkeypatch.setattr(auth_router, "SessionLocal", demo_db)
    with pytest.raises(HTTPException) as error:
        auth_router.get_principal(credentials=_bearer(mint_token()))
    assert error.value.status_code == 401


def test_existing_local_jwt_still_resolves_principal(demo_db, monkeypatch):
    monkeypatch.setattr(auth_router, "SessionLocal", demo_db)
    principal = auth_router.get_principal(
        credentials=_bearer(create_access_token({"sub": "1"}))
    )
    assert principal.id == 1


OwnedBase = declarative_base()


class ExampleResource(OwnedBase):
    __tablename__ = "oidc_test_resource"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False)
    label = Column(String, nullable=False)


def test_cross_user_query_remains_scoped_by_local_principal():
    """Unit check of the ownership helper; route-level tests remain separate."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    OwnedBase.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            db.add_all(
                [
                    ExampleResource(id=1, user_id=1, label="mine"),
                    ExampleResource(id=2, user_id=2, label="other"),
                ]
            )
            db.commit()
            rows = owned(
                db.query(ExampleResource),
                ExampleResource.user_id,
                Subject(1),
            ).all()
            assert [row.id for row in rows] == [1]
    finally:
        engine.dispose()
