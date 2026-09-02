from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import DBAPIError

from app.canonical_audit import audit_user, find_first_causal_divergence
from app.credit_card_service import canonical_account_position, credit_card_liability
from app.database import Base, SessionLocal, engine
from app.main import app
from app.models import (
    CommandReceipt,
    FinancialEvent,
    FinancialEventEntry,
    FinancialEventHistory,
    FinancialEventLink,
)

client = TestClient(app)
Base.metadata.create_all(bind=engine)


def create_user(prefix="causal"):
    email = f"{prefix}_{uuid4().hex}@example.com"
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


def with_key(headers, key=None, expected_version=None):
    result = dict(headers)
    result["Idempotency-Key"] = key or uuid4().hex
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


def create_expense(headers, account_id, amount="1000000.00"):
    response = client.post(
        "/transactions",
        json={
            "amount": amount,
            "description": "purchase",
            "category": "shopping",
            "type": "expense",
            "account_id": account_id,
        },
        headers=with_key(headers),
    )
    assert response.status_code == 200
    return response


def create_income(headers, account_id, amount="1000000.00"):
    response = client.post(
        "/transactions",
        json={
            "amount": amount,
            "description": "salary",
            "category": "income",
            "type": "income",
            "account_id": account_id,
        },
        headers=with_key(headers),
    )
    assert response.status_code == 200
    return response


def create_transfer(headers, from_account_id, to_account_id, amount="500000.00"):
    response = client.post(
        "/transfers",
        json={
            "amount": amount,
            "from_account_id": from_account_id,
            "to_account_id": to_account_id,
            "description": "move funds",
        },
        headers=with_key(headers),
    )
    assert response.status_code == 200
    return response


def test_partial_refund_is_new_linked_event_and_reduces_expense_not_income():
    user_id, headers = create_user()
    bank = create_account(headers, name="Bank")
    purchase = create_expense(headers, bank["id"], "1000000.00")
    original_event_id = purchase.json()["canonical_event_id"]

    before = client.get("/summary", headers=headers).json()
    assert Decimal(str(before["total_expense"])) == Decimal("1000000.00")

    refund = client.post(
        "/refunds",
        json={
            "original_event_id": original_event_id,
            "amount": "250000.00",
            "description": "partial merchant refund",
        },
        headers=with_key(headers),
    )
    assert refund.status_code == 200
    body = refund.json()
    assert body["event_type"] == "REFUND"
    assert body["relation_type"] == "REFUND_OF"
    assert body["original_event_id"] == original_event_id
    assert body["entries"] == [
        {"account_id": bank["id"], "amount": "250000.00"}
    ]

    summary = client.get("/summary", headers=headers).json()
    assert Decimal(str(summary["total_income"])) == Decimal("0")
    assert Decimal(str(summary["total_expense"])) == Decimal("750000.00")
    assert Decimal(str(summary["balance"])) == Decimal("-750000.00")

    db = SessionLocal()
    try:
        original = db.query(FinancialEvent).filter(FinancialEvent.id == original_event_id).one()
        assert original.event_type == "EXPENSE"
        assert original.version == 1
        assert original.lifecycle_state == "ACTIVE"
        link = db.query(FinancialEventLink).filter(FinancialEventLink.from_event_id == body["id"]).one()
        assert link.to_event_id == original_event_id
        assert link.relation_type == "REFUND_OF"
        history = db.query(FinancialEventHistory).filter(FinancialEventHistory.financial_event_id == body["id"]).one()
        assert history.transition_type == "CREATED"
        assert history.event_version == 1
        report = audit_user(db, user_id)
        assert report["summary_match"] is True
        assert report["causal_divergence"] is None
        assert report["economic_summary"]["total_expense"] == "750000.00"
    finally:
        db.close()


def test_multiple_partial_refunds_cannot_exceed_original_expense():
    _, headers = create_user()
    bank = create_account(headers, name="Bank")
    purchase = create_expense(headers, bank["id"], "1000000.00")
    original = purchase.json()["canonical_event_id"]

    first = client.post(
        "/refunds",
        json={"original_event_id": original, "amount": "400000.00"},
        headers=with_key(headers),
    )
    second = client.post(
        "/refunds",
        json={"original_event_id": original, "amount": "600000.00"},
        headers=with_key(headers),
    )
    excess = client.post(
        "/refunds",
        json={"original_event_id": original, "amount": "1.00"},
        headers=with_key(headers),
    )
    assert first.status_code == 200
    assert second.status_code == 200
    assert excess.status_code == 422

    summary = client.get("/summary", headers=headers).json()
    assert Decimal(str(summary["total_expense"])) == Decimal("0.00")


def test_credit_card_refund_reduces_liability_without_becoming_income():
    user_id, headers = create_user()
    card = create_account(headers, name="Card", account_type="CREDIT_CARD")
    purchase = create_expense(headers, card["id"], "800000.00")

    refund = client.post(
        "/refunds",
        json={
            "original_event_id": purchase.json()["canonical_event_id"],
            "amount": "300000.00",
        },
        headers=with_key(headers),
    )
    assert refund.status_code == 200

    db = SessionLocal()
    try:
        assert canonical_account_position(
            db, user_id=user_id, account_id=card["id"]
        ) == Decimal("-500000.00")
        assert credit_card_liability(
            db, user_id=user_id, account_id=card["id"]
        ) == Decimal("500000.00")
    finally:
        db.close()

    summary = client.get("/summary", headers=headers).json()
    assert Decimal(str(summary["total_income"])) == Decimal("0")
    assert Decimal(str(summary["total_expense"])) == Decimal("500000.00")


def test_refund_rejects_non_expense_original_and_cross_user_original():
    _, owner_headers = create_user("owner")
    _, other_headers = create_user("other")
    bank = create_account(owner_headers, name="Bank")
    income = create_income(owner_headers, bank["id"], "100000.00")

    wrong_type = client.post(
        "/refunds",
        json={
            "original_event_id": income.json()["canonical_event_id"],
            "amount": "10000.00",
        },
        headers=with_key(owner_headers),
    )
    hidden_foreign = client.post(
        "/refunds",
        json={
            "original_event_id": income.json()["canonical_event_id"],
            "amount": "10000.00",
        },
        headers=with_key(other_headers),
    )
    assert wrong_type.status_code == 422
    assert hidden_foreign.status_code == 422


def test_refund_command_is_idempotent_and_key_cannot_change_payload():
    user_id, headers = create_user()
    bank = create_account(headers, name="Bank")
    purchase = create_expense(headers, bank["id"], "500000.00")
    original = purchase.json()["canonical_event_id"]
    key = uuid4().hex

    payload = {"original_event_id": original, "amount": "100000.00"}
    first = client.post("/refunds", json=payload, headers=with_key(headers, key))
    second = client.post("/refunds", json=payload, headers=with_key(headers, key))
    conflict = client.post(
        "/refunds",
        json={"original_event_id": original, "amount": "200000.00"},
        headers=with_key(headers, key),
    )
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == first.json()
    assert conflict.status_code == 409

    db = SessionLocal()
    try:
        assert db.query(FinancialEvent).filter(
            FinancialEvent.user_id == user_id,
            FinancialEvent.event_type == "REFUND",
        ).count() == 1
        assert db.query(CommandReceipt).filter(
            CommandReceipt.user_id == user_id,
            CommandReceipt.command_type == "CREATE_REFUND",
        ).count() == 1
    finally:
        db.close()


def test_expense_reversal_exactly_cancels_effect_without_deleting_original():
    user_id, headers = create_user()
    bank = create_account(headers, name="Bank")
    purchase = create_expense(headers, bank["id"], "700000.00")
    original_id = purchase.json()["canonical_event_id"]

    reversal = client.post(
        "/reversals",
        json={"original_event_id": original_id, "description": "merchant void"},
        headers=with_key(headers),
    )
    assert reversal.status_code == 200
    body = reversal.json()
    assert body["event_type"] == "REVERSAL"
    assert body["relation_type"] == "REVERSAL_OF"
    assert body["entries"] == [
        {"account_id": bank["id"], "amount": "700000.00"}
    ]

    summary = client.get("/summary", headers=headers).json()
    assert Decimal(str(summary["total_expense"])) == Decimal("0.00")
    assert Decimal(str(summary["balance"])) == Decimal("0.00")

    db = SessionLocal()
    try:
        original = db.query(FinancialEvent).filter(FinancialEvent.id == original_id).one()
        assert original.event_type == "EXPENSE"
        assert original.lifecycle_state == "ACTIVE"
        assert original.version == 1
        assert db.query(FinancialEvent).filter(FinancialEvent.id == body["id"]).one().event_type == "REVERSAL"
        assert audit_user(db, user_id)["causal_divergence"] is None
    finally:
        db.close()


def test_income_reversal_reduces_income_to_zero():
    _, headers = create_user()
    bank = create_account(headers, name="Bank")
    income = create_income(headers, bank["id"], "1500000.00")

    reversal = client.post(
        "/reversals",
        json={"original_event_id": income.json()["canonical_event_id"]},
        headers=with_key(headers),
    )
    assert reversal.status_code == 200
    assert reversal.json()["entries"] == [
        {"account_id": bank["id"], "amount": "-1500000.00"}
    ]
    summary = client.get("/summary", headers=headers).json()
    assert Decimal(str(summary["total_income"])) == Decimal("0.00")
    assert Decimal(str(summary["balance"])) == Decimal("0.00")


def test_transfer_reversal_is_exact_inverse_and_preserves_summary():
    user_id, headers = create_user()
    a = create_account(headers, name="A")
    b = create_account(headers, name="B")
    transfer = create_transfer(headers, a["id"], b["id"], "350000.00")

    reversal = client.post(
        "/reversals",
        json={"original_event_id": transfer.json()["id"]},
        headers=with_key(headers),
    )
    assert reversal.status_code == 200
    entries = {
        (row["account_id"], Decimal(row["amount"]))
        for row in reversal.json()["entries"]
    }
    assert entries == {
        (a["id"], Decimal("350000.00")),
        (b["id"], Decimal("-350000.00")),
    }
    summary = client.get("/summary", headers=headers).json()
    assert Decimal(str(summary["total_income"])) == 0
    assert Decimal(str(summary["total_expense"])) == 0

    db = SessionLocal()
    try:
        assert canonical_account_position(db, user_id=user_id, account_id=a["id"]) == 0
        assert canonical_account_position(db, user_id=user_id, account_id=b["id"]) == 0
    finally:
        db.close()


def test_refund_and_reversal_are_mutually_exclusive_for_same_original():
    _, headers = create_user()
    bank = create_account(headers, name="Bank")
    p1 = create_expense(headers, bank["id"], "100000.00")
    p2 = create_expense(headers, bank["id"], "100000.00")

    refund = client.post(
        "/refunds",
        json={"original_event_id": p1.json()["canonical_event_id"], "amount": "10000.00"},
        headers=with_key(headers),
    )
    assert refund.status_code == 200
    reversal_after_refund = client.post(
        "/reversals",
        json={"original_event_id": p1.json()["canonical_event_id"]},
        headers=with_key(headers),
    )
    assert reversal_after_refund.status_code == 422

    reversal = client.post(
        "/reversals",
        json={"original_event_id": p2.json()["canonical_event_id"]},
        headers=with_key(headers),
    )
    assert reversal.status_code == 200
    refund_after_reversal = client.post(
        "/refunds",
        json={"original_event_id": p2.json()["canonical_event_id"], "amount": "10000.00"},
        headers=with_key(headers),
    )
    assert refund_after_reversal.status_code == 422


def test_second_reversal_is_rejected():
    _, headers = create_user()
    bank = create_account(headers, name="Bank")
    purchase = create_expense(headers, bank["id"], "100000.00")
    original = purchase.json()["canonical_event_id"]
    first = client.post(
        "/reversals",
        json={"original_event_id": original},
        headers=with_key(headers),
    )
    second = client.post(
        "/reversals",
        json={"original_event_id": original},
        headers=with_key(headers),
    )
    assert first.status_code == 200
    assert second.status_code == 422


def test_original_cannot_be_corrected_or_voided_after_causal_child_exists():
    _, headers = create_user()
    bank = create_account(headers, name="Bank")
    purchase = create_expense(headers, bank["id"], "200000.00")
    body = purchase.json()
    refund = client.post(
        "/refunds",
        json={"original_event_id": body["canonical_event_id"], "amount": "50000.00"},
        headers=with_key(headers),
    )
    assert refund.status_code == 200

    update = client.put(
        f"/transactions/{body['id']}",
        json={
            "amount": "300000.00",
            "description": "changed after refund",
            "category": "shopping",
            "type": "expense",
            "account_id": bank["id"],
        },
        headers=with_key(headers, expected_version=body["canonical_version"]),
    )
    delete = client.delete(
        f"/transactions/{body['id']}",
        headers=with_key(headers, expected_version=body["canonical_version"]),
    )
    assert update.status_code == 409
    assert delete.status_code == 409


def test_audit_detects_causal_amount_tampering_without_polluting_database():
    user_id, headers = create_user()
    bank = create_account(headers, name="Bank")
    purchase = create_expense(headers, bank["id"], "300000.00")
    reversal = client.post(
        "/reversals",
        json={"original_event_id": purchase.json()["canonical_event_id"]},
        headers=with_key(headers),
    )
    assert reversal.status_code == 200

    db = SessionLocal()
    try:
        entry = db.query(FinancialEventEntry).filter(
            FinancialEventEntry.financial_event_id == reversal.json()["id"]
        ).one()
        entry.amount = Decimal("1.00")
        db.flush()
        divergence = find_first_causal_divergence(db, user_id)
        assert divergence is not None
        assert divergence["reason"] == "reversal_amount_not_exact_inverse"
        db.rollback()
        assert audit_user(db, user_id)["causal_divergence"] is None
    finally:
        db.rollback()
        db.close()


def test_financial_event_links_are_append_only_in_postgresql():
    if engine.dialect.name != "postgresql":
        pytest.skip("Database-level financial_event_links trigger is PostgreSQL-specific")

    _, headers = create_user()
    bank = create_account(headers, name="Bank")
    purchase = create_expense(headers, bank["id"], "100000.00")
    refund = client.post(
        "/refunds",
        json={"original_event_id": purchase.json()["canonical_event_id"], "amount": "10000.00"},
        headers=with_key(headers),
    )
    assert refund.status_code == 200

    db = SessionLocal()
    try:
        link = db.query(FinancialEventLink).filter(
            FinancialEventLink.from_event_id == refund.json()["id"]
        ).one()
        link.relation_type = "REVERSAL_OF"
        with pytest.raises(DBAPIError):
            db.commit()
    finally:
        db.rollback()
        db.close()
