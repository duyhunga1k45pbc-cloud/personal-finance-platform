from decimal import Decimal
from uuid import uuid4

from fastapi.testclient import TestClient

from app.canonical_audit import audit_user, find_first_credit_card_divergence
from app.credit_card_service import canonical_account_position, credit_card_liability
from app.database import Base, SessionLocal, engine
from app.main import app
from app.models import FinancialEvent, FinancialEventEntry, FinancialEventHistory, Transaction

client = TestClient(app)
Base.metadata.create_all(bind=engine)


def create_user():
    email = f"card_{uuid4().hex}@example.com"
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


def create_account(headers, *, name, account_type):
    response = client.post(
        "/accounts",
        json={"name": name, "account_type": account_type, "currency": "VND"},
        headers=headers,
    )
    assert response.status_code == 200
    return response.json()


def expense_payload(account_id, amount="1000000.00"):
    return {
        "amount": amount,
        "description": "credit card purchase",
        "category": "shopping",
        "type": "expense",
        "account_id": account_id,
    }


def transfer_payload(from_account_id, to_account_id, amount="1000000.00"):
    return {
        "amount": amount,
        "from_account_id": from_account_id,
        "to_account_id": to_account_id,
        "description": "credit card repayment",
    }


def test_credit_card_purchase_is_expense_and_increases_liability():
    user_id, headers = create_user()
    card = create_account(headers, name="Visa", account_type="CREDIT_CARD")

    response = client.post(
        "/transactions",
        json=expense_payload(card["id"], "1200000.00"),
        headers=with_key(headers),
    )
    assert response.status_code == 200

    db = SessionLocal()
    try:
        transaction_id = response.json()["id"]
        event = (
            db.query(FinancialEvent)
            .filter(FinancialEvent.legacy_transaction_id == transaction_id)
            .one()
        )
        entry = (
            db.query(FinancialEventEntry)
            .filter(FinancialEventEntry.financial_event_id == event.id)
            .one()
        )
        assert event.event_type == "EXPENSE"
        assert entry.account_id == card["id"]
        assert Decimal(entry.amount) == Decimal("-1200000.00")
        assert canonical_account_position(
            db, user_id=user_id, account_id=card["id"]
        ) == Decimal("-1200000.00")
        assert credit_card_liability(
            db, user_id=user_id, account_id=card["id"]
        ) == Decimal("1200000.00")
    finally:
        db.close()

    summary = client.get("/summary", headers=headers)
    assert summary.status_code == 200
    assert Decimal(str(summary.json()["total_expense"])) == Decimal("1200000.00")
    assert Decimal(str(summary.json()["balance"])) == Decimal("-1200000.00")


def test_credit_card_repayment_is_transfer_and_does_not_double_count_expense():
    user_id, headers = create_user()
    bank = create_account(headers, name="Bank", account_type="BANK")
    card = create_account(headers, name="Card", account_type="CREDIT_CARD")

    purchase = client.post(
        "/transactions",
        json=expense_payload(card["id"], "900000.00"),
        headers=with_key(headers),
    )
    assert purchase.status_code == 200
    before_repayment = client.get("/summary", headers=headers).json()

    repayment = client.post(
        "/transfers",
        json=transfer_payload(bank["id"], card["id"], "900000.00"),
        headers=with_key(headers),
    )
    assert repayment.status_code == 200
    after_repayment = client.get("/summary", headers=headers).json()
    assert after_repayment == before_repayment
    assert Decimal(str(after_repayment["total_expense"])) == Decimal("900000.00")

    db = SessionLocal()
    try:
        event = (
            db.query(FinancialEvent)
            .filter(FinancialEvent.id == repayment.json()["id"])
            .one()
        )
        entries = (
            db.query(FinancialEventEntry)
            .filter(FinancialEventEntry.financial_event_id == event.id)
            .all()
        )
        assert event.event_type == "TRANSFER"
        assert {(row.account_id, Decimal(row.amount)) for row in entries} == {
            (bank["id"], Decimal("-900000.00")),
            (card["id"], Decimal("900000.00")),
        }
        assert canonical_account_position(
            db, user_id=user_id, account_id=card["id"]
        ) == Decimal("0.00")
        assert credit_card_liability(
            db, user_id=user_id, account_id=card["id"]
        ) == Decimal("0")
    finally:
        db.close()


def test_partial_credit_card_repayment_reduces_liability_not_expense():
    user_id, headers = create_user()
    bank = create_account(headers, name="Bank", account_type="BANK")
    card = create_account(headers, name="Card", account_type="CREDIT_CARD")

    purchase = client.post(
        "/transactions",
        json=expense_payload(card["id"], "1000000.00"),
        headers=with_key(headers),
    )
    assert purchase.status_code == 200
    repayment = client.post(
        "/transfers",
        json=transfer_payload(bank["id"], card["id"], "400000.00"),
        headers=with_key(headers),
    )
    assert repayment.status_code == 200

    db = SessionLocal()
    try:
        assert canonical_account_position(
            db, user_id=user_id, account_id=card["id"]
        ) == Decimal("-600000.00")
        assert credit_card_liability(
            db, user_id=user_id, account_id=card["id"]
        ) == Decimal("600000.00")
    finally:
        db.close()

    summary = client.get("/summary", headers=headers)
    assert summary.status_code == 200
    assert Decimal(str(summary.json()["total_expense"])) == Decimal("1000000.00")


def test_income_cannot_be_recorded_directly_on_credit_card():
    user_id, headers = create_user()
    card = create_account(headers, name="Card", account_type="CREDIT_CARD")

    response = client.post(
        "/transactions",
        json={
            "amount": "100000.00",
            "description": "not a valid refund",
            "category": "refund",
            "type": "income",
            "account_id": card["id"],
        },
        headers=with_key(headers),
    )
    assert response.status_code == 422

    db = SessionLocal()
    try:
        assert db.query(Transaction).filter(Transaction.user_id == user_id).count() == 0
        assert db.query(FinancialEvent).filter(FinancialEvent.user_id == user_id).count() == 0
    finally:
        db.close()


def test_update_cannot_turn_credit_card_purchase_into_income():
    user_id, headers = create_user()
    card = create_account(headers, name="Card", account_type="CREDIT_CARD")
    created = client.post(
        "/transactions",
        json=expense_payload(card["id"], "500000.00"),
        headers=with_key(headers),
    )
    assert created.status_code == 200

    response = client.put(
        f"/transactions/{created.json()['id']}",
        json={
            "amount": "500000.00",
            "description": "invalid income correction",
            "category": "refund",
            "type": "income",
            "account_id": card["id"],
        },
        headers=with_key(
            headers,
            expected_version=created.json()["canonical_version"],
        ),
    )
    assert response.status_code == 422

    db = SessionLocal()
    try:
        transaction = (
            db.query(Transaction)
            .filter(Transaction.id == created.json()["id"])
            .one()
        )
        event = (
            db.query(FinancialEvent)
            .filter(FinancialEvent.legacy_transaction_id == transaction.id)
            .one()
        )
        history = (
            db.query(FinancialEventHistory)
            .filter(FinancialEventHistory.financial_event_id == event.id)
            .all()
        )
        assert transaction.type == "expense"
        assert event.event_type == "EXPENSE"
        assert event.version == 1
        assert len(history) == 1
    finally:
        db.close()


def test_credit_card_cash_advance_can_be_transfer_without_becoming_expense():
    user_id, headers = create_user()
    card = create_account(headers, name="Card", account_type="CREDIT_CARD")
    bank = create_account(headers, name="Bank", account_type="BANK")

    before = client.get("/summary", headers=headers).json()
    response = client.post(
        "/transfers",
        json=transfer_payload(card["id"], bank["id"], "300000.00"),
        headers=with_key(headers),
    )
    assert response.status_code == 200
    assert client.get("/summary", headers=headers).json() == before

    db = SessionLocal()
    try:
        assert canonical_account_position(
            db, user_id=user_id, account_id=card["id"]
        ) == Decimal("-300000.00")
        assert credit_card_liability(
            db, user_id=user_id, account_id=card["id"]
        ) == Decimal("300000.00")
    finally:
        db.close()


def test_credit_card_audit_detects_invalid_income_without_polluting_database():
    user_id, headers = create_user()
    card = create_account(headers, name="Card", account_type="CREDIT_CARD")
    created = client.post(
        "/transactions",
        json=expense_payload(card["id"], "250000.00"),
        headers=with_key(headers),
    )
    assert created.status_code == 200

    db = SessionLocal()
    try:
        event = (
            db.query(FinancialEvent)
            .filter(FinancialEvent.legacy_transaction_id == created.json()["id"])
            .one()
        )
        event.event_type = "INCOME"
        db.flush()

        divergence = find_first_credit_card_divergence(db, user_id)
        assert divergence is not None
        assert divergence["reason"] == "income_on_credit_card"
        db.rollback()

        clean = audit_user(db, user_id)
        assert clean["ok"] is True
        assert clean["credit_card_divergence"] is None
    finally:
        db.rollback()
        db.close()
