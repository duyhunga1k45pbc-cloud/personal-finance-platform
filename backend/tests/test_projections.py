from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.canonical_audit import audit_user, find_first_projection_divergence
from app.canonical_service import canonical_summary
from app.database import SessionLocal
from app.main import app
from app.models import (
    FinancialAccountBalanceProjection,
    FinancialProjectionState,
    User,
)
from app.projection_service import (
    canonical_account_balances,
    canonical_projection_fingerprint,
    rebuild_user_projection,
)


client = TestClient(app)


def create_user(prefix="projection"):
    email = f"{prefix}-{uuid4().hex}@example.com"
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
    return register.json()["id"], {
        "Authorization": f"Bearer {login.json()['access_token']}"
    }


def command_headers(headers, *, expected_version=None):
    result = dict(headers)
    result["Idempotency-Key"] = uuid4().hex
    if expected_version is not None:
        result["X-Expected-Version"] = str(expected_version)
    return result


def create_account(headers, *, name, account_type="BANK"):
    response = client.post(
        "/accounts",
        json={"name": name, "account_type": account_type, "currency": "VND"},
        headers=headers,
    )
    assert response.status_code == 200
    return response.json()


def create_transaction(headers, *, amount, transaction_type, account_id=None, description="projection test"):
    payload = {
        "amount": str(amount),
        "description": description,
        "category": "test",
        "type": transaction_type,
    }
    if account_id is not None:
        payload["account_id"] = account_id
    response = client.post(
        "/transactions",
        json=payload,
        headers=command_headers(headers),
    )
    assert response.status_code == 200
    return response.json()


def rebuild(headers):
    response = client.post("/projections/rebuild", headers=headers)
    assert response.status_code == 200
    return response.json()


def test_projection_is_missing_until_explicit_rebuild():
    _, headers = create_user()
    status = client.get("/projections/status", headers=headers)
    assert status.status_code == 200
    assert status.json()["status"] == "MISSING"

    summary = client.get("/projections/summary", headers=headers)
    assert summary.status_code == 409
    assert summary.json()["detail"] == "projection_missing"


def test_rebuild_creates_zero_projection_for_default_cash_account():
    user_id, headers = create_user()
    body = rebuild(headers)
    assert body["generation"] == 1
    assert Decimal(str(body["total_income"])) == Decimal("0")
    assert Decimal(str(body["total_expense"])) == Decimal("0")
    assert Decimal(str(body["balance"])) == Decimal("0")
    assert Decimal(str(body["net_worth"])) == Decimal("0")
    assert body["account_count"] == 1

    status = client.get("/projections/status", headers=headers).json()
    assert status["status"] == "FRESH"

    db = SessionLocal()
    try:
        rows = db.query(FinancialAccountBalanceProjection).filter(
            FinancialAccountBalanceProjection.user_id == user_id
        ).all()
        assert len(rows) == 1
        assert Decimal(rows[0].balance) == Decimal("0")
    finally:
        db.close()


def test_projection_matches_canonical_summary_and_account_balances():
    user_id, headers = create_user()
    bank = create_account(headers, name="Projection bank")
    create_transaction(headers, amount="1000000.00", transaction_type="income", account_id=bank["id"])
    create_transaction(headers, amount="125000.25", transaction_type="expense", account_id=bank["id"])

    body = rebuild(headers)
    db = SessionLocal()
    try:
        summary = canonical_summary(db, user_id)
        balances = canonical_account_balances(db, user_id)
        assert Decimal(str(body["total_income"])) == summary.total_income
        assert Decimal(str(body["total_expense"])) == summary.total_expense
        assert Decimal(str(body["balance"])) == summary.balance
        assert Decimal(str(body["net_worth"])) == sum(balances.values(), Decimal("0"))
    finally:
        db.close()


def test_transfer_changes_account_projection_but_not_net_worth():
    _, headers = create_user()
    source = create_account(headers, name="Source")
    destination = create_account(headers, name="Destination", account_type="EWALLET")
    created = client.post(
        "/transfers",
        json={
            "amount": "250000.00",
            "from_account_id": source["id"],
            "to_account_id": destination["id"],
            "description": "projection transfer",
        },
        headers=command_headers(headers),
    )
    assert created.status_code == 200

    projection = rebuild(headers)
    assert Decimal(str(projection["net_worth"])) == Decimal("0")
    rows = client.get("/projections/accounts", headers=headers)
    assert rows.status_code == 200
    balances = {row["account_id"]: Decimal(str(row["balance"])) for row in rows.json()}
    assert balances[source["id"]] == Decimal("-250000.00")
    assert balances[destination["id"]] == Decimal("250000.00")


def test_canonical_write_marks_projection_stale_and_projection_read_fails_closed():
    _, headers = create_user()
    rebuild(headers)
    create_transaction(headers, amount="50000.00", transaction_type="expense")

    status = client.get("/projections/status", headers=headers)
    assert status.status_code == 200
    assert status.json()["status"] == "STALE"

    read = client.get("/projections/summary", headers=headers)
    assert read.status_code == 409
    assert read.json()["detail"] == "projection_stale"


def test_rebuild_after_stale_state_advances_generation_and_becomes_fresh():
    _, headers = create_user()
    first = rebuild(headers)
    create_transaction(headers, amount="75000.00", transaction_type="expense")
    second = rebuild(headers)

    assert first["generation"] == 1
    assert second["generation"] == 2
    assert first["canonical_fingerprint"] != second["canonical_fingerprint"]
    assert client.get("/projections/status", headers=headers).json()["status"] == "FRESH"


def test_delete_projection_rows_then_rebuild_reconstructs_same_values():
    user_id, headers = create_user()
    create_transaction(headers, amount="123456.78", transaction_type="income")
    first = rebuild(headers)

    db = SessionLocal()
    try:
        db.query(FinancialAccountBalanceProjection).filter(
            FinancialAccountBalanceProjection.user_id == user_id
        ).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()

    db = SessionLocal()
    try:
        divergence = find_first_projection_divergence(db, user_id)
        assert divergence is not None
        assert divergence["reason"] == "projection_account_row_count_mismatch"
    finally:
        db.close()

    second = rebuild(headers)
    assert second["generation"] == first["generation"] + 1
    assert Decimal(str(second["total_income"])) == Decimal(str(first["total_income"]))
    assert Decimal(str(second["net_worth"])) == Decimal(str(first["net_worth"]))


def test_projection_corruption_is_detected_by_audit_without_changing_canonical_truth():
    user_id, headers = create_user()
    create_transaction(headers, amount="100000.00", transaction_type="income")
    rebuild(headers)

    db = SessionLocal()
    try:
        row = db.query(FinancialAccountBalanceProjection).filter(
            FinancialAccountBalanceProjection.user_id == user_id
        ).first()
        row.balance = Decimal("999999.00")
        db.flush()
        divergence = find_first_projection_divergence(db, user_id)
        assert divergence is not None
        assert divergence["reason"] == "projection_account_balance_mismatch"
        db.rollback()

        assert audit_user(db, user_id)["projection_divergence"] is None
    finally:
        db.close()


def test_rebuild_repairs_committed_projection_corruption():
    user_id, headers = create_user()
    create_transaction(headers, amount="90000.00", transaction_type="expense")
    rebuild(headers)

    db = SessionLocal()
    try:
        state = db.query(FinancialProjectionState).filter(
            FinancialProjectionState.user_id == user_id
        ).one()
        state.net_worth = Decimal("42.00")
        db.commit()
    finally:
        db.close()

    db = SessionLocal()
    try:
        assert find_first_projection_divergence(db, user_id)["reason"] == "projection_scalar_mismatch"
    finally:
        db.close()

    rebuild(headers)
    db = SessionLocal()
    try:
        assert find_first_projection_divergence(db, user_id) is None
    finally:
        db.close()


def test_projection_endpoints_are_user_isolated():
    user_a, headers_a = create_user("projection-a")
    user_b, headers_b = create_user("projection-b")
    create_transaction(headers_a, amount="111000.00", transaction_type="income")
    create_transaction(headers_b, amount="222000.00", transaction_type="income")
    rebuild(headers_a)
    rebuild(headers_b)

    a = client.get("/projections/summary", headers=headers_a)
    b = client.get("/projections/summary", headers=headers_b)
    assert Decimal(str(a.json()["total_income"])) == Decimal("111000.00")
    assert Decimal(str(b.json()["total_income"])) == Decimal("222000.00")

    db = SessionLocal()
    try:
        assert db.query(FinancialProjectionState).filter(
            FinancialProjectionState.user_id == user_a
        ).count() == 1
        assert db.query(FinancialProjectionState).filter(
            FinancialProjectionState.user_id == user_b
        ).count() == 1
    finally:
        db.close()


def test_failed_rebuild_rolls_back_delete_and_does_not_publish_partial_generation():
    user_id, headers = create_user()
    first = rebuild(headers)
    create_transaction(headers, amount="5000.00", transaction_type="expense")

    db = SessionLocal()
    try:
        with patch.object(db, "commit", side_effect=RuntimeError("simulated commit failure")):
            with pytest.raises(RuntimeError, match="simulated commit failure"):
                rebuild_user_projection(db, user_id)
    finally:
        db.close()

    verify = SessionLocal()
    try:
        state = verify.query(FinancialProjectionState).filter(
            FinancialProjectionState.user_id == user_id
        ).one()
        rows = verify.query(FinancialAccountBalanceProjection).filter(
            FinancialAccountBalanceProjection.user_id == user_id
        ).all()
        assert state.generation == first["generation"]
        assert rows
        # Canonical state changed, so the retained old generation is correctly stale.
        assert state.canonical_fingerprint != canonical_projection_fingerprint(verify, user_id)
    finally:
        verify.close()


def test_event_correction_changes_projection_fingerprint_even_when_projection_table_is_untouched():
    user_id, headers = create_user()
    created = create_transaction(
        headers,
        amount="100000.00",
        transaction_type="expense",
        description="before correction",
    )
    rebuilt = rebuild(headers)

    update = client.put(
        f"/transactions/{created['id']}",
        json={
            "amount": "100000.00",
            "description": "after correction",
            "category": "test",
            "type": "expense",
            "account_id": created["account_id"],
        },
        headers=command_headers(headers, expected_version=created["canonical_version"]),
    )
    assert update.status_code == 200

    db = SessionLocal()
    try:
        current_fingerprint = canonical_projection_fingerprint(db, user_id)
        assert current_fingerprint != rebuilt["canonical_fingerprint"]
    finally:
        db.close()
    assert client.get("/projections/status", headers=headers).json()["status"] == "STALE"
