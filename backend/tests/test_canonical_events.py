from decimal import Decimal
from uuid import uuid4

from fastapi.testclient import TestClient

from app.canonical_audit import audit_user
from app.database import Base, SessionLocal, engine
from app.main import app
from app.models import FinancialEvent, FinancialEventEntry, Transaction, User

client = TestClient(app)
Base.metadata.create_all(bind=engine)


def create_user():
    email = f"canonical_{uuid4().hex}@example.com"
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
    return register.json()["id"], {"Authorization": f"Bearer {token}"}


def test_create_transaction_dual_writes_canonical_event_and_signed_entry():
    user_id, headers = create_user()

    response = client.post(
        "/transactions",
        json={
            "amount": "125000.50",
            "description": "salary fragment",
            "category": "income",
            "type": "income",
        },
        headers=headers,
    )
    assert response.status_code == 200
    transaction_id = response.json()["id"]

    db = SessionLocal()
    try:
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
        transaction = db.query(Transaction).filter(Transaction.id == transaction_id).one()

        assert event.user_id == user_id
        assert event.event_type == "INCOME"
        assert event.description == transaction.description
        assert event.category == transaction.category
        assert event.occurred_at == transaction.date
        assert entry.account_id == transaction.account_id
        assert Decimal(entry.amount) == Decimal("125000.50")
    finally:
        db.close()


def test_expense_entry_is_negative():
    _, headers = create_user()

    response = client.post(
        "/transactions",
        json={
            "amount": "50000.00",
            "description": "lunch",
            "category": "food",
            "type": "expense",
        },
        headers=headers,
    )
    assert response.status_code == 200
    transaction_id = response.json()["id"]

    db = SessionLocal()
    try:
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
        assert Decimal(entry.amount) == Decimal("-50000.00")
    finally:
        db.close()


def test_update_transaction_updates_same_canonical_event():
    _, headers = create_user()

    created = client.post(
        "/transactions",
        json={
            "amount": "100000.00",
            "description": "before",
            "category": "misc",
            "type": "expense",
        },
        headers=headers,
    )
    transaction_id = created.json()["id"]

    db = SessionLocal()
    try:
        original_event = (
            db.query(FinancialEvent)
            .filter(FinancialEvent.legacy_transaction_id == transaction_id)
            .one()
        )
        original_event_id = original_event.id
    finally:
        db.close()

    updated = client.put(
        f"/transactions/{transaction_id}",
        json={
            "amount": "250000.00",
            "description": "after",
            "category": "salary",
            "type": "income",
        },
        headers=headers,
    )
    assert updated.status_code == 200

    db = SessionLocal()
    try:
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
        assert event.id == original_event_id
        assert event.event_type == "INCOME"
        assert event.description == "after"
        assert event.category == "salary"
        assert Decimal(entry.amount) == Decimal("250000.00")
    finally:
        db.close()


def test_delete_transaction_removes_task2_canonical_mirror():
    _, headers = create_user()

    created = client.post(
        "/transactions",
        json={
            "amount": "30000.00",
            "description": "temporary",
            "category": "misc",
            "type": "expense",
        },
        headers=headers,
    )
    transaction_id = created.json()["id"]

    deleted = client.delete(f"/transactions/{transaction_id}", headers=headers)
    assert deleted.status_code == 200

    db = SessionLocal()
    try:
        assert (
            db.query(FinancialEvent)
            .filter(FinancialEvent.legacy_transaction_id == transaction_id)
            .count()
            == 0
        )
    finally:
        db.close()


def test_legacy_and_canonical_summaries_are_equal():
    user_id, headers = create_user()

    for payload in [
        {
            "amount": "1000000.00",
            "description": "income",
            "category": "salary",
            "type": "income",
        },
        {
            "amount": "125000.25",
            "description": "expense 1",
            "category": "food",
            "type": "expense",
        },
        {
            "amount": "50000.50",
            "description": "expense 2",
            "category": "transport",
            "type": "expense",
        },
    ]:
        response = client.post("/transactions", json=payload, headers=headers)
        assert response.status_code == 200

    db = SessionLocal()
    try:
        result = audit_user(db, user_id)
        assert result["ok"] is True
        assert result["summary_match"] is True
        assert result["first_divergence"] is None
        assert result["legacy"]["balance"] == "824999.25"
        assert result["canonical"]["balance"] == "824999.25"
    finally:
        db.close()


def test_audit_reports_first_divergence():
    user_id, headers = create_user()

    response = client.post(
        "/transactions",
        json={
            "amount": "75000.00",
            "description": "audit test",
            "category": "food",
            "type": "expense",
        },
        headers=headers,
    )
    transaction_id = response.json()["id"]

    db = SessionLocal()
    try:
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
        entry.amount = Decimal("-1.00")
        db.commit()

        result = audit_user(db, user_id)
        assert result["ok"] is False
        assert result["first_divergence"]["legacy_transaction_id"] == transaction_id
        assert result["first_divergence"]["reason"] == "amount_mismatch"
    finally:
        db.close()
