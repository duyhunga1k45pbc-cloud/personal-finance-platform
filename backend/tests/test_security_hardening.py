from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from jose import jwt

from app.auth import (
    ALGORITHM,
    JWT_AUDIENCE,
    JWT_ISSUER,
    SECRET_KEY,
)
from app.database import Base, engine
from app.main import app
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


def test_token_for_nonexistent_principal_is_401_not_404(client):
    user_id, _ = _register_and_login(client)
    nonexistent = _manual_token(subject=str(user_id + 999999))

    response = client.get(
        "/summary",
        headers={"Authorization": f"Bearer {nonexistent}"},
    )
    _assert_unauthorized(response)


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
