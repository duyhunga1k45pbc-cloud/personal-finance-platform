from uuid import uuid4

from fastapi.testclient import TestClient

from app.main import app
from app.database import Base, engine


client = TestClient(app)


Base.metadata.create_all(bind=engine)


def get_auth_headers():
    email = f"test_{uuid4().hex}@example.com"
    password = "testpassword123"

    client.post(
        "/auth/register",
        json={
            "email": email,
            "password": password,
        },
    )

    login_response = client.post(
        "/auth/login",
        json={
            "email": email,
            "password": password,
        },
    )

    assert login_response.status_code == 200

    token = login_response.json()["access_token"]

    return {
        "Authorization": f"Bearer {token}"
    }


def test_transactions_requires_auth():
    response = client.get("/transactions")

    assert response.status_code == 401


def test_create_transaction():
    headers = get_auth_headers()

    response = client.post(
        "/transactions",
        json={
            "amount": 50000,
            "description": "test lunch",
            "category": "food",
            "type": "expense",
        },
        headers=headers,
    )

    assert response.status_code == 200

    data = response.json()
    assert data["amount"] == 50000
    assert data["description"] == "test lunch"
    assert data["category"] == "food"
    assert data["type"] == "expense"


def test_create_transaction_with_invalid_amount():
    headers = get_auth_headers()

    response = client.post(
        "/transactions",
        json={
            "amount": 0,
            "description": "invalid amount",
            "category": "food",
            "type": "expense",
        },
        headers=headers,
    )

    assert response.status_code == 422


def test_create_transaction_with_invalid_type():
    headers = get_auth_headers()

    response = client.post(
        "/transactions",
        json={
            "amount": 50000,
            "description": "invalid type",
            "category": "food",
            "type": "abc",
        },
        headers=headers,
    )

    assert response.status_code == 422