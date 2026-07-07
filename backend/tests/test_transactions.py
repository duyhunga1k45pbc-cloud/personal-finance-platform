from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


def test_home():
    response = client.get("/")

    assert response.status_code == 200
    assert "message" in response.json()

def test_create_transaction():
    response = client.post(
        "/transactions",
        json={
            "amount": 50000,
            "description": "test lunch",
            "category": "food",
            "type": "expense"
        }
    )

    assert response.status_code == 200

    data = response.json()
    assert data["amount"] == 50000
    assert data["description"] == "test lunch"
    assert data["category"] == "food"
    assert data["type"] == "expense"
    assert "id" in data

def test_create_transaction_with_invalid_amount():
    response = client.post(
        "/transactions",
        json={
            "amount": 0,
            "description": "invalid amount",
            "category": "food",
            "type": "expense"
        }
    )

    assert response.status_code == 422
def test_create_transaction_with_invalid_type():
    response = client.post(
        "/transactions",
        json={
            "amount": 50000,
            "description": "invalid type",
            "category": "food",
            "type": "abc"
        }
    )

    assert response.status_code == 422