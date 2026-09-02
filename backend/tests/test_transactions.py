from decimal import Decimal
from uuid import uuid4

from fastapi.testclient import TestClient

from app.main import app
from app.database import Base, SessionLocal, engine
from app.models import Transaction


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
    assert data["account_id"] is not None


def test_amount_is_stored_as_decimal_numeric():
    headers = get_auth_headers()
    response = client.post(
        "/transactions",
        json={
            "amount": "12345.67",
            "description": "decimal test",
            "category": "test",
            "type": "expense",
        },
        headers=headers,
    )
    assert response.status_code == 200

    transaction_id = response.json()["id"]
    db = SessionLocal()
    try:
        row = db.query(Transaction).filter(Transaction.id == transaction_id).one()
        assert isinstance(row.amount, Decimal)
        assert row.amount == Decimal("12345.67")
    finally:
        db.close()


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
