from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


def create_user(prefix: str):
    email = f"{prefix}-{uuid4().hex}@example.com"
    password = "testpassword123"
    registered = client.post(
        "/auth/register", json={"email": email, "password": password}
    )
    assert registered.status_code == 200
    logged_in = client.post(
        "/auth/login", json={"email": email, "password": password}
    )
    assert logged_in.status_code == 200
    return {
        "Authorization": f"Bearer {logged_in.json()['access_token']}"
    }


def create_transaction(headers, amount: str, transaction_type: str, description: str):
    response = client.post(
        "/transactions",
        json={
            "amount": amount,
            "type": transaction_type,
            "description": description,
            "category": "test",
        },
        headers={**headers, "Idempotency-Key": uuid4().hex},
    )
    assert response.status_code == 200


def create_income(headers, amount: str, description: str):
    create_transaction(headers, amount, "income", description)


def rebuild(headers):
    response = client.post("/projections/rebuild", headers=headers)
    assert response.status_code == 200


def test_analysis_context_rejects_missing_and_stale_projections():
    headers = create_user("analysis-freshness")

    missing = client.get("/analytics/context", headers=headers)
    assert missing.status_code == 409
    assert missing.json()["detail"] == "projection_missing"

    rebuild(headers)
    create_income(headers, "100.00", "private description must not appear")

    stale = client.get("/analytics/context", headers=headers)
    assert stale.status_code == 409
    assert stale.json()["detail"] == "projection_stale"


def test_analysis_context_is_owner_scoped_and_snapshot_bound():
    headers_a = create_user("analysis-owner-a")
    headers_b = create_user("analysis-owner-b")
    create_income(headers_a, "111.00", "owner A private text")
    create_income(headers_b, "222.00", "owner B private text")
    rebuild(headers_a)
    rebuild(headers_b)

    a = client.get("/analytics/context", headers=headers_a)
    b = client.get("/analytics/context", headers=headers_b)
    assert a.status_code == b.status_code == 200
    context_a, context_b = a.json(), b.json()
    assert context_a["metrics"]["total_income"] == "111.00"
    assert context_b["metrics"]["total_income"] == "222.00"
    assert context_a["snapshot_id"] != context_b["snapshot_id"]
    repeated_a = client.get("/analytics/context", headers=headers_a).json()
    assert repeated_a["snapshot_id"] == context_a["snapshot_id"]
    assert [item["id"] for item in repeated_a["evidence"]] == [
        item["id"] for item in context_a["evidence"]
    ]
    evidence_a = {item["id"] for item in context_a["evidence"]}
    evidence_b = {item["id"] for item in context_b["evidence"]}
    assert evidence_a.isdisjoint(evidence_b)
    assert "owner A private text" not in a.text + b.text
    assert "owner B private text" not in a.text + b.text
    assert "access_token" not in a.text + b.text


def test_analysis_context_reports_projection_values_and_rounds_ratio():
    headers = create_user("analysis-metrics")
    create_transaction(headers, "32.00", "income", "income private text")
    create_transaction(headers, "1.00", "expense", "expense private text")
    rebuild(headers)

    result = client.get("/analytics/context", headers=headers)
    assert result.status_code == 200
    assert result.json()["metrics"] == {
        "total_income": "32.00",
        "total_expense": "1.00",
        "balance": "31.00",
        "net_worth": "31.00",
        "account_count": 1,
        "expense_to_income_percent": "3.13",
    }
