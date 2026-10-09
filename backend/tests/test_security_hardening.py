from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy.orm import Session

from app.auth import (
    ALGORITHM,
    JWT_AUDIENCE,
    JWT_ISSUER,
    SECRET_KEY,
)
from app.database import Base, SessionLocal, engine
from app.main import app
from app.models import (
    CommandReceipt,
    FinancialEvent,
    FinancialEventEntry,
    FinancialEventHistory,
    Transaction,
    User,
)
from app.security import SecurityConfigurationError, validate_security_values

pytestmark = pytest.mark.security


def _clear_app_data():
    # Security tests must isolate DATA, not destroy the migrated SCHEMA.
    # PostgreSQL TRUNCATE preserves constraints, triggers, and Alembic-managed
    # database objects while removing rows between tests.
    if engine.dialect.name == "postgresql":
        table_names = [
            engine.dialect.identifier_preparer.quote(table.name)
            for table in Base.metadata.sorted_tables
        ]
        if table_names:
            with engine.begin() as connection:
                connection.exec_driver_sql(
                    "TRUNCATE TABLE "
                    + ", ".join(table_names)
                    + " RESTART IDENTITY CASCADE"
                )
        return

    # SQLite/local fallback: delete children before parents.
    with engine.begin() as connection:
        for table in reversed(Base.metadata.sorted_tables):
            connection.execute(table.delete())


@pytest.fixture(autouse=True)
def reset_database():
    Base.metadata.create_all(bind=engine)
    _clear_app_data()
    yield
    _clear_app_data()


@pytest.fixture
def client():
    return TestClient(app)


def _register_and_login(client: TestClient, *, email: str = "security@example.com"):
    password = "testpassword123"
    registered = client.post(
        "/auth/register",
        json={"email": email, "password": password},
    )
    assert registered.status_code == 200, registered.text
    logged_in = client.post(
        "/auth/login",
        json={"email": email, "password": password},
    )
    assert logged_in.status_code == 200, logged_in.text
    return registered.json()["id"], logged_in.json()["access_token"]


def _assert_unauthorized(response):
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["detail"] == "Invalid authentication credentials"


def _manual_token(*, subject: str, **overrides):
    now = datetime.now(timezone.utc)
    claims = {
        "sub": subject,
        "type": "access",
        "iss": JWT_ISSUER,
        "aud": JWT_AUDIENCE,
        "iat": now,
        "nbf": now,
        "exp": now + timedelta(minutes=5),
        "jti": "security-test-jti",
    }
    claims.update(overrides)
    return jwt.encode(claims, SECRET_KEY, algorithm=ALGORITHM)


def _authorization(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _command_headers(token: str) -> dict[str, str]:
    return {
        **_authorization(token),
        "Idempotency-Key": uuid4().hex,
    }


def _transaction_payload(
    *,
    amount: str = "125000.00",
    description: str = "security isolation",
    account_id: int | None = None,
) -> dict:
    payload = {
        "amount": amount,
        "description": description,
        "category": "test",
        "type": "expense",
    }
    if account_id is not None:
        payload["account_id"] = account_id
    return payload


def test_production_rejects_missing_placeholder_and_short_secrets():
    for secret in (
        None,
        "",
        "secret",
        "your-secret-key-change-this-later",
        "short-but-not-placeholder",
    ):
        with pytest.raises(SecurityConfigurationError):
            validate_security_values(
                app_env="production",
                secret_key=secret,
                jwt_algorithm="HS256",
                access_token_expire_minutes=60,
            )

    validate_security_values(
        app_env="production",
        secret_key="a" * 32,
        jwt_algorithm="HS256",
        access_token_expire_minutes=60,
    )


def test_disallowed_jwt_algorithm_fails_configuration():
    with pytest.raises(SecurityConfigurationError):
        validate_security_values(
            app_env="production",
            secret_key="a" * 32,
            jwt_algorithm="none",
            access_token_expire_minutes=60,
        )


def test_registration_rejects_weak_password_and_unexpected_fields(client):
    weak = client.post(
        "/auth/register",
        json={"email": "weak@example.com", "password": "1234567"},
    )
    assert weak.status_code == 422

    extra = client.post(
        "/auth/register",
        json={
            "email": "extra@example.com",
            "password": "testpassword123",
            "is_admin": True,
        },
    )
    assert extra.status_code == 422


def test_email_identity_is_case_insensitive(client):
    first = client.post(
        "/auth/register",
        json={"email": "Case.User@Example.com", "password": "testpassword123"},
    )
    assert first.status_code == 200
    assert first.json()["email"] == "case.user@example.com"

    duplicate = client.post(
        "/auth/register",
        json={"email": "CASE.USER@example.com", "password": "testpassword123"},
    )
    assert duplicate.status_code == 409

    login = client.post(
        "/auth/login",
        json={"email": "CASE.USER@EXAMPLE.COM", "password": "testpassword123"},
    )
    assert login.status_code == 200


def test_missing_and_malformed_bearer_credentials_are_401(client):
    _assert_unauthorized(client.get("/summary"))
    _assert_unauthorized(
        client.get(
            "/summary",
            headers={"Authorization": "Bearer definitely-not-a-jwt"},
        )
    )


def test_wrong_audience_issuer_and_token_type_are_rejected(client):
    user_id, _ = _register_and_login(client)

    wrong_audience = _manual_token(subject=str(user_id), aud="other-client")
    wrong_issuer = _manual_token(subject=str(user_id), iss="other-issuer")
    wrong_type = _manual_token(subject=str(user_id), type="refresh")

    for token in (wrong_audience, wrong_issuer, wrong_type):
        _assert_unauthorized(
            client.get(
                "/summary",
                headers={"Authorization": f"Bearer {token}"},
            )
        )


def test_expired_token_is_rejected(client):
    user_id, _ = _register_and_login(client)
    now = datetime.now(timezone.utc)
    expired = _manual_token(
        subject=str(user_id),
        iat=now - timedelta(minutes=10),
        nbf=now - timedelta(minutes=10),
        exp=now - timedelta(minutes=1),
    )
    _assert_unauthorized(
        client.get(
            "/summary",
            headers={"Authorization": f"Bearer {expired}"},
        )
    )


def test_tampered_jwt_signature_and_payload_are_rejected(client):
    user_id, token = _register_and_login(client)
    header_segment, payload_segment, signature_segment = token.split(".")

    signature_padding = "=" * (-len(signature_segment) % 4)
    signature_bytes = bytearray(
        base64.urlsafe_b64decode(signature_segment + signature_padding)
    )
    signature_bytes[0] ^= 1
    assert bytes(signature_bytes) != base64.urlsafe_b64decode(
        signature_segment + signature_padding
    )
    tampered_signature_segment = (
        base64.urlsafe_b64encode(signature_bytes).rstrip(b"=").decode()
    )
    tampered_signature = ".".join(
        (header_segment, payload_segment, tampered_signature_segment)
    )

    padding = "=" * (-len(payload_segment) % 4)
    payload = json.loads(
        base64.urlsafe_b64decode(payload_segment + padding)
    )
    payload["sub"] = str(user_id + 999999)
    encoded_payload = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).rstrip(b"=").decode()
    tampered_payload = ".".join(
        (header_segment, encoded_payload, signature_segment)
    )

    for altered_token in (tampered_signature, tampered_payload):
        _assert_unauthorized(
            client.get("/auth/me", headers=_authorization(altered_token))
        )


def test_token_for_nonexistent_principal_is_401_not_404(client):
    user_id, _ = _register_and_login(client)
    nonexistent = _manual_token(subject=str(user_id + 999999))

    response = client.get(
        "/summary",
        headers={"Authorization": f"Bearer {nonexistent}"},
    )
    _assert_unauthorized(response)


def test_token_for_deleted_principal_is_401(client):
    db: Session = SessionLocal()
    user = User(
        email=f"deleted_{uuid4().hex}@example.com",
        hashed_password="unused-security-test-hash",
    )
    try:
        db.add(user)
        db.commit()
        user_id = user.id
        token = _manual_token(subject=str(user_id))

        db.delete(user)
        db.commit()
    finally:
        db.close()

    _assert_unauthorized(
        client.get("/auth/me", headers=_authorization(token))
    )


def test_cross_user_transaction_reads_are_hidden(client):
    owner_id, owner_token = _register_and_login(
        client, email="transaction-owner@example.com"
    )
    _, other_token = _register_and_login(
        client, email="transaction-reader@example.com"
    )
    created = client.post(
        "/transactions",
        json=_transaction_payload(),
        headers=_command_headers(owner_token),
    )
    assert created.status_code == 200, created.text
    transaction_id = created.json()["id"]

    listed = client.get(
        "/transactions",
        headers=_authorization(other_token),
    )
    assert listed.status_code == 200, listed.text
    assert listed.json()["total"] == 0
    assert listed.json()["data"] == []

    for path in (
        f"/transactions/{transaction_id}",
        f"/transactions/{transaction_id}/history",
    ):
        response = client.get(path, headers=_authorization(other_token))
        assert response.status_code == 404, response.text

    db: Session = SessionLocal()
    try:
        stored = db.query(Transaction).filter(Transaction.id == transaction_id).one()
        assert stored.user_id == owner_id
    finally:
        db.close()


def test_cross_user_transaction_writes_have_no_side_effects(client):
    owner_id, owner_token = _register_and_login(
        client, email="write-owner@example.com"
    )
    attacker_id, attacker_token = _register_and_login(
        client, email="write-attacker@example.com"
    )

    owner_account = client.post(
        "/accounts",
        json={
            "name": "Private account",
            "account_type": "BANK",
            "currency": "VND",
        },
        headers=_authorization(owner_token),
    )
    assert owner_account.status_code == 200, owner_account.text
    account_id = owner_account.json()["id"]
    owner_transaction = client.post(
        "/transactions",
        json=_transaction_payload(account_id=account_id),
        headers=_command_headers(owner_token),
    )
    assert owner_transaction.status_code == 200, owner_transaction.text
    transaction_id = owner_transaction.json()["id"]
    event_id = owner_transaction.json()["canonical_event_id"]

    rejected_create = client.post(
        "/transactions",
        json=_transaction_payload(
            amount="999999.00",
            description="unauthorized create",
            account_id=account_id,
        ),
        headers=_command_headers(attacker_token),
    )
    assert rejected_create.status_code == 404, rejected_create.text

    update_headers = _command_headers(attacker_token)
    update_headers["X-Expected-Version"] = str(
        owner_transaction.json()["canonical_version"]
    )
    rejected_update = client.put(
        f"/transactions/{transaction_id}",
        json=_transaction_payload(
            amount="777777.00",
            description="unauthorized update",
            account_id=account_id,
        ),
        headers=update_headers,
    )
    assert rejected_update.status_code == 404, rejected_update.text

    delete_headers = _command_headers(attacker_token)
    delete_headers["X-Expected-Version"] = str(
        owner_transaction.json()["canonical_version"]
    )
    rejected_delete = client.delete(
        f"/transactions/{transaction_id}",
        headers=delete_headers,
    )
    assert rejected_delete.status_code == 404, rejected_delete.text

    db: Session = SessionLocal()
    try:
        stored_transaction = (
            db.query(Transaction).filter(Transaction.id == transaction_id).one()
        )
        stored_event = db.query(FinancialEvent).filter(FinancialEvent.id == event_id).one()
        history = (
            db.query(FinancialEventHistory)
            .filter(FinancialEventHistory.financial_event_id == event_id)
            .all()
        )
        entries = (
            db.query(FinancialEventEntry)
            .filter(FinancialEventEntry.financial_event_id == event_id)
            .all()
        )
        attacker_receipts = (
            db.query(CommandReceipt)
            .filter(CommandReceipt.user_id == attacker_id)
            .count()
        )
        attacker_transactions = (
            db.query(Transaction)
            .filter(Transaction.user_id == attacker_id)
            .count()
        )
        attacker_events = (
            db.query(FinancialEvent)
            .filter(FinancialEvent.user_id == attacker_id)
            .count()
        )

        assert stored_transaction.user_id == owner_id
        assert stored_transaction.amount == 125000
        assert stored_transaction.description == "security isolation"
        assert stored_event.user_id == owner_id
        assert stored_event.lifecycle_state == "ACTIVE"
        assert stored_event.version == 1
        assert len(history) == 1
        assert history[0].transition_type == "CREATED"
        assert len(entries) == 1
        assert attacker_receipts == 0
        assert attacker_transactions == 0
        assert attacker_events == 0
    finally:
        db.close()


def test_sql_injection_shaped_login_does_not_bypass_auth(client):
    _register_and_login(client)
    response = client.post(
        "/auth/login",
        json={
            "email": "security@example.com' OR '1'='1",
            "password": "anything123",
        },
    )
    # Email validation rejects this before it reaches the parameterized ORM
    # query; either way it cannot authenticate.
    assert response.status_code in (401, 422)
    assert "access_token" not in response.text


def test_security_headers_are_present_on_public_and_authenticated_responses(client):
    home = client.get("/")
    assert home.status_code == 200

    _, token = _register_and_login(client)
    summary = client.get(
        "/summary",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert summary.status_code == 200

    for response in (home, summary):
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["pragma"] == "no-cache"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["referrer-policy"] == "no-referrer"


def test_every_non_public_api_operation_declares_bearer_security():
    schema = app.openapi()
    public = {
        ("get", "/"),
        ("get", "/health/live"),
        ("get", "/health/ready"),
        ("post", "/auth/register"),
        ("post", "/auth/login"),
    }

    missing = []
    for path, path_item in schema["paths"].items():
        for method, operation in path_item.items():
            if method.lower() not in {
                "get", "post", "put", "patch", "delete", "options", "head"
            }:
                continue
            key = (method.lower(), path)
            if key in public:
                continue
            if not operation.get("security"):
                missing.append(key)

    assert missing == [], f"Protected API operations missing Bearer security: {missing}"
