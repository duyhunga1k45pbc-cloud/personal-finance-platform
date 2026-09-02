from uuid import uuid4

from fastapi.testclient import TestClient

from app.database import Base, engine
from app.main import app

client = TestClient(app)
Base.metadata.create_all(bind=engine)


def create_user():
    email = f"account_{uuid4().hex}@example.com"
    password = "testpassword123"

    register = client.post(
        "/auth/register",
        json={"email": email, "password": password},
    )
    assert register.status_code == 200

    login = client.post(
        "/auth/login",
        json={"email": email, "password": password},
    )
    assert login.status_code == 200

    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    return headers


def command_headers(headers):
    result = dict(headers)
    result["Idempotency-Key"] = uuid4().hex
    return result


def test_registration_creates_one_default_vnd_cash_account():
    headers = create_user()

    response = client.get("/accounts", headers=headers)
    assert response.status_code == 200

    accounts = response.json()
    defaults = [account for account in accounts if account["is_default"]]

    assert len(defaults) == 1
    assert defaults[0]["name"] == "Default Cash"
    assert defaults[0]["account_type"] == "CASH"
    assert defaults[0]["currency"] == "VND"


def test_v1_rejects_non_vnd_account():
    headers = create_user()

    response = client.post(
        "/accounts",
        json={
            "name": "USD Wallet",
            "account_type": "EWALLET",
            "currency": "USD",
        },
        headers=headers,
    )

    assert response.status_code == 422


def test_user_cannot_read_another_users_account():
    user_a = create_user()
    user_b = create_user()

    created = client.post(
        "/accounts",
        json={
            "name": "VCB",
            "account_type": "BANK",
            "currency": "VND",
        },
        headers=user_a,
    )
    assert created.status_code == 200
    account_id = created.json()["id"]

    response = client.get(f"/accounts/{account_id}", headers=user_b)
    assert response.status_code == 404


def test_user_cannot_create_transaction_on_another_users_account():
    user_a = create_user()
    user_b = create_user()

    created = client.post(
        "/accounts",
        json={
            "name": "MoMo",
            "account_type": "EWALLET",
            "currency": "VND",
        },
        headers=user_a,
    )
    assert created.status_code == 200
    account_id = created.json()["id"]

    response = client.post(
        "/transactions",
        json={
            "amount": "100000.00",
            "description": "ownership attack",
            "category": "test",
            "type": "expense",
            "account_id": account_id,
        },
        headers=command_headers(user_b),
    )

    assert response.status_code == 404


def test_transaction_without_account_uses_users_default_cash():
    headers = create_user()

    accounts_response = client.get("/accounts", headers=headers)
    default_account = next(
        account
        for account in accounts_response.json()
        if account["is_default"]
    )

    transaction = client.post(
        "/transactions",
        json={
            "amount": "50000.00",
            "description": "cash lunch",
            "category": "food",
            "type": "expense",
        },
        headers=command_headers(headers),
    )

    assert transaction.status_code == 200
    assert transaction.json()["account_id"] == default_account["id"]
