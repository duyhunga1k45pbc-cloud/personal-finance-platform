from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import DBAPIError

from app.canonical_audit import audit_user
from app.database import Base, SessionLocal, engine
from app.main import app
from app.models import (
    FinancialEvent,
    FinancialEventEntry,
    FinancialEventHistory,
    Transaction,
)

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


def command_headers(headers, *, expected_version=None, idempotency_key=None):
    result = dict(headers)
    result["Idempotency-Key"] = idempotency_key or uuid4().hex
    if expected_version is not None:
        result["X-Expected-Version"] = str(expected_version)
    return result


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
        headers=command_headers(headers),
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
        assert event.lifecycle_state == "ACTIVE"
        assert event.version == 1
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
        headers=command_headers(headers),
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


def test_create_appends_created_history():
    user_id, headers = create_user()
    created = client.post(
        "/transactions",
        json={
            "amount": "80000.00",
            "description": "history create",
            "category": "food",
            "type": "expense",
        },
        headers=command_headers(headers),
    )
    transaction_id = created.json()["id"]

    db = SessionLocal()
    try:
        event = (
            db.query(FinancialEvent)
            .filter(FinancialEvent.legacy_transaction_id == transaction_id)
            .one()
        )
        rows = (
            db.query(FinancialEventHistory)
            .filter(FinancialEventHistory.financial_event_id == event.id)
            .order_by(FinancialEventHistory.event_version)
            .all()
        )
        assert len(rows) == 1
        assert rows[0].transition_type == "CREATED"
        assert rows[0].event_version == 1
        assert rows[0].actor_type == "USER"
        assert rows[0].actor_user_id == user_id
        assert rows[0].previous_state is None
        assert rows[0].new_state["entries"][0]["amount"] == "-80000.00"
    finally:
        db.close()


def test_update_transaction_appends_correction_without_destroying_history():
    user_id, headers = create_user()

    created = client.post(
        "/transactions",
        json={
            "amount": "100000.00",
            "description": "before",
            "category": "misc",
            "type": "expense",
        },
        headers=command_headers(headers),
    )
    transaction_id = created.json()["id"]

    updated = client.put(
        f"/transactions/{transaction_id}",
        json={
            "amount": "250000.00",
            "description": "after",
            "category": "salary",
            "type": "income",
        },
        headers=command_headers(
            headers, expected_version=created.json()["canonical_version"]
        ),
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
        history = (
            db.query(FinancialEventHistory)
            .filter(FinancialEventHistory.financial_event_id == event.id)
            .order_by(FinancialEventHistory.event_version.asc())
            .all()
        )

        assert event.event_type == "INCOME"
        assert event.version == 2
        assert Decimal(entry.amount) == Decimal("250000.00")
        assert [row.transition_type for row in history] == ["CREATED", "CORRECTED"]
        assert history[1].actor_user_id == user_id
        assert history[1].previous_state["event_type"] == "EXPENSE"
        assert history[1].previous_state["entries"][0]["amount"] == "-100000.00"
        assert history[1].new_state["event_type"] == "INCOME"
        assert history[1].new_state["entries"][0]["amount"] == "250000.00"
    finally:
        db.close()


def test_idempotent_update_does_not_create_fake_history_version():
    _, headers = create_user()
    payload = {
        "amount": "100000.00",
        "description": "same",
        "category": "misc",
        "type": "expense",
    }
    created = client.post("/transactions", json=payload, headers=command_headers(headers))
    transaction_id = created.json()["id"]

    updated = client.put(
        f"/transactions/{transaction_id}",
        json=payload,
        headers=command_headers(
            headers, expected_version=created.json()["canonical_version"]
        ),
    )
    assert updated.status_code == 200

    db = SessionLocal()
    try:
        event = (
            db.query(FinancialEvent)
            .filter(FinancialEvent.legacy_transaction_id == transaction_id)
            .one()
        )
        history_count = (
            db.query(FinancialEventHistory)
            .filter(FinancialEventHistory.financial_event_id == event.id)
            .count()
        )
        assert event.version == 1
        assert history_count == 1
    finally:
        db.close()


def test_delete_voids_event_and_preserves_evidence_and_history():
    _, headers = create_user()

    created = client.post(
        "/transactions",
        json={
            "amount": "30000.00",
            "description": "temporary",
            "category": "misc",
            "type": "expense",
        },
        headers=command_headers(headers),
    )
    transaction_id = created.json()["id"]

    deleted = client.delete(
        f"/transactions/{transaction_id}",
        headers=command_headers(
            headers, expected_version=created.json()["canonical_version"]
        ),
    )
    assert deleted.status_code == 200
    assert client.get(f"/transactions/{transaction_id}", headers=headers).status_code == 404

    listed = client.get("/transactions", headers=headers)
    assert all(row["id"] != transaction_id for row in listed.json()["data"])

    summary = client.get("/summary", headers=headers)
    assert summary.status_code == 200
    assert Decimal(str(summary.json()["total_expense"])) == Decimal("0")

    db = SessionLocal()
    try:
        legacy = db.query(Transaction).filter(Transaction.id == transaction_id).one()
        event = (
            db.query(FinancialEvent)
            .filter(FinancialEvent.legacy_transaction_id == transaction_id)
            .one()
        )
        history = (
            db.query(FinancialEventHistory)
            .filter(FinancialEventHistory.financial_event_id == event.id)
            .order_by(FinancialEventHistory.event_version.asc())
            .all()
        )
        assert legacy.id == transaction_id
        assert event.lifecycle_state == "VOIDED"
        assert event.version == 2
        assert [row.transition_type for row in history] == ["CREATED", "VOIDED"]
    finally:
        db.close()


def test_history_endpoint_remains_available_after_void():
    _, headers = create_user()
    created = client.post(
        "/transactions",
        json={
            "amount": "40000.00",
            "description": "history endpoint",
            "category": "misc",
            "type": "expense",
        },
        headers=command_headers(headers),
    )
    transaction_id = created.json()["id"]
    client.delete(
        f"/transactions/{transaction_id}",
        headers=command_headers(
            headers, expected_version=created.json()["canonical_version"]
        ),
    )

    response = client.get(f"/transactions/{transaction_id}/history", headers=headers)
    assert response.status_code == 200
    assert [row["transition_type"] for row in response.json()] == ["CREATED", "VOIDED"]


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
        response = client.post("/transactions", json=payload, headers=command_headers(headers))
        assert response.status_code == 200

    db = SessionLocal()
    try:
        result = audit_user(db, user_id)
        assert result["ok"] is True
        assert result["summary_match"] is True
        assert result["first_divergence"] is None
        assert result["history_divergence"] is None
        assert result["legacy"]["balance"] == "824999.25"
        assert result["canonical"]["balance"] == "824999.25"
    finally:
        db.close()


def test_audit_reports_first_divergence_without_polluting_test_database():
    user_id, headers = create_user()

    response = client.post(
        "/transactions",
        json={
            "amount": "75000.00",
            "description": "audit test",
            "category": "food",
            "type": "expense",
        },
        headers=command_headers(headers),
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
        db.flush()

        result = audit_user(db, user_id)
        assert result["ok"] is False
        assert result["first_divergence"]["legacy_transaction_id"] == transaction_id
        assert result["first_divergence"]["reason"] == "amount_mismatch"
    finally:
        db.rollback()
        db.close()


def test_audit_detects_current_state_changed_without_history():
    user_id, headers = create_user()
    response = client.post(
        "/transactions",
        json={
            "amount": "90000.00",
            "description": "history audit",
            "category": "food",
            "type": "expense",
        },
        headers=command_headers(headers),
    )
    transaction_id = response.json()["id"]

    db = SessionLocal()
    try:
        event = (
            db.query(FinancialEvent)
            .filter(FinancialEvent.legacy_transaction_id == transaction_id)
            .one()
        )
        event.category = "tampered"
        db.flush()

        result = audit_user(db, user_id)
        assert result["ok"] is False
        assert result["history_divergence"]["reason"] == "latest_history_snapshot_mismatch"
    finally:
        db.rollback()
        db.close()


def test_history_table_is_append_only_in_postgresql():
    if engine.dialect.name != "postgresql":
        pytest.skip("Database-level append-only trigger is PostgreSQL-specific")

    _, headers = create_user()
    created = client.post(
        "/transactions",
        json={
            "amount": "10000.00",
            "description": "immutable history",
            "category": "test",
            "type": "expense",
        },
        headers=command_headers(headers),
    )
    transaction_id = created.json()["id"]

    db = SessionLocal()
    try:
        event = (
            db.query(FinancialEvent)
            .filter(FinancialEvent.legacy_transaction_id == transaction_id)
            .one()
        )
        history = (
            db.query(FinancialEventHistory)
            .filter(FinancialEventHistory.financial_event_id == event.id)
            .one()
        )
        history.reason = "tamper"
        with pytest.raises(DBAPIError):
            db.commit()
    finally:
        db.rollback()
        db.close()
