from decimal import Decimal
from uuid import uuid4

from fastapi.testclient import TestClient

from app.canonical_audit import audit_user, find_first_transfer_divergence
from app.canonical_service import canonical_summary
from app.database import Base, SessionLocal, engine
from app.main import app
from app.models import (
    CommandReceipt,
    FinancialAccount,
    FinancialEvent,
    FinancialEventEntry,
    FinancialEventHistory,
    Transaction,
)

client = TestClient(app)
Base.metadata.create_all(bind=engine)


def create_user():
    email = f"transfer_{uuid4().hex}@example.com"
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


def with_key(headers, key=None):
    result = dict(headers)
    result["Idempotency-Key"] = key or uuid4().hex
    return result


def create_account(headers, *, name, account_type="BANK"):
    response = client.post(
        "/accounts",
        json={"name": name, "account_type": account_type, "currency": "VND"},
        headers=headers,
    )
    assert response.status_code == 200
    return response.json()


def transfer_payload(from_account_id, to_account_id, amount="1000000.00"):
    return {
        "amount": amount,
        "from_account_id": from_account_id,
        "to_account_id": to_account_id,
        "description": "move money",
    }


def test_transfer_creates_one_event_with_two_zero_sum_entries_and_no_legacy_row():
    user_id, headers = create_user()
    source = create_account(headers, name="Source bank")
    destination = create_account(headers, name="Destination wallet", account_type="EWALLET")

    db = SessionLocal()
    try:
        legacy_before = db.query(Transaction).filter(Transaction.user_id == user_id).count()
    finally:
        db.close()

    response = client.post(
        "/transfers",
        json=transfer_payload(source["id"], destination["id"]),
        headers=with_key(headers),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["event_type"] == "TRANSFER"
    assert Decimal(str(body["amount"])) == Decimal("1000000.00")
    assert body["from_account_id"] == source["id"]
    assert body["to_account_id"] == destination["id"]
    assert body["canonical_version"] == 1

    db = SessionLocal()
    try:
        event = db.query(FinancialEvent).filter(FinancialEvent.id == body["id"]).one()
        entries = (
            db.query(FinancialEventEntry)
            .filter(FinancialEventEntry.financial_event_id == event.id)
            .order_by(FinancialEventEntry.id.asc())
            .all()
        )
        assert event.user_id == user_id
        assert event.event_type == "TRANSFER"
        assert event.legacy_transaction_id is None
        assert len(entries) == 2
        assert {(entry.account_id, Decimal(entry.amount)) for entry in entries} == {
            (source["id"], Decimal("-1000000.00")),
            (destination["id"], Decimal("1000000.00")),
        }
        assert sum((Decimal(entry.amount) for entry in entries), Decimal("0")) == 0
        assert db.query(Transaction).filter(Transaction.user_id == user_id).count() == legacy_before
    finally:
        db.close()


def test_transfer_does_not_create_income_or_expense_and_preserves_net_effect():
    user_id, headers = create_user()
    source = create_account(headers, name="A")
    destination = create_account(headers, name="B")

    before_response = client.get("/summary", headers=headers)
    assert before_response.status_code == 200

    created = client.post(
        "/transfers",
        json=transfer_payload(source["id"], destination["id"], "250000.00"),
        headers=with_key(headers),
    )
    assert created.status_code == 200

    after_response = client.get("/summary", headers=headers)
    assert after_response.status_code == 200
    assert after_response.json() == before_response.json()

    db = SessionLocal()
    try:
        summary = canonical_summary(db, user_id)
        assert summary.total_income == Decimal("0")
        assert summary.total_expense == Decimal("0")
        assert summary.balance == Decimal("0")
    finally:
        db.close()


def test_transfer_appends_created_history_with_both_entries():
    user_id, headers = create_user()
    source = create_account(headers, name="History source")
    destination = create_account(headers, name="History destination")

    created = client.post(
        "/transfers",
        json=transfer_payload(source["id"], destination["id"], "90000.00"),
        headers=with_key(headers),
    )
    assert created.status_code == 200

    db = SessionLocal()
    try:
        event_id = created.json()["id"]
        history = (
            db.query(FinancialEventHistory)
            .filter(FinancialEventHistory.financial_event_id == event_id)
            .one()
        )
        assert history.transition_type == "CREATED"
        assert history.event_version == 1
        assert history.actor_type == "USER"
        assert history.actor_user_id == user_id
        assert history.previous_state is None
        assert history.new_state["event_type"] == "TRANSFER"
        amounts = {entry["amount"] for entry in history.new_state["entries"]}
        assert amounts == {"-90000.00", "90000.00"}
    finally:
        db.close()


def test_transfer_rejects_same_source_and_destination():
    _, headers = create_user()
    account = create_account(headers, name="Only account")

    response = client.post(
        "/transfers",
        json=transfer_payload(account["id"], account["id"]),
        headers=with_key(headers),
    )
    assert response.status_code == 422


def test_transfer_fee_cannot_be_hidden_inside_transfer_payload():
    _, headers = create_user()
    source = create_account(headers, name="Fee source")
    destination = create_account(headers, name="Fee destination")
    payload = transfer_payload(source["id"], destination["id"], "100000.00")
    payload["fee_amount"] = "11000.00"

    response = client.post(
        "/transfers",
        json=payload,
        headers=with_key(headers),
    )
    assert response.status_code == 422


def test_transfer_cannot_reference_another_users_account():
    _, owner_headers = create_user()
    _, attacker_headers = create_user()
    owned_by_other = create_account(owner_headers, name="Other user bank")
    attacker_destination = create_account(attacker_headers, name="My bank")

    response = client.post(
        "/transfers",
        json=transfer_payload(owned_by_other["id"], attacker_destination["id"]),
        headers=with_key(attacker_headers),
    )
    assert response.status_code == 404


def test_same_transfer_command_replays_without_duplicate_event():
    user_id, headers = create_user()
    source = create_account(headers, name="Idem source")
    destination = create_account(headers, name="Idem destination")
    key = uuid4().hex
    payload = transfer_payload(source["id"], destination["id"], "777000.00")

    first = client.post("/transfers", json=payload, headers=with_key(headers, key))
    replay = client.post("/transfers", json=payload, headers=with_key(headers, key))

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json() == first.json()

    db = SessionLocal()
    try:
        event_id = first.json()["id"]
        assert (
            db.query(FinancialEvent)
            .filter(
                FinancialEvent.id == event_id,
                FinancialEvent.user_id == user_id,
                FinancialEvent.event_type == "TRANSFER",
            )
            .count()
            == 1
        )
        receipt = (
            db.query(CommandReceipt)
            .filter(
                CommandReceipt.user_id == user_id,
                CommandReceipt.command_type == "CREATE_TRANSFER",
                CommandReceipt.idempotency_key == key,
            )
            .one()
        )
        assert receipt.transaction_id is None
        assert receipt.financial_event_id == event_id
    finally:
        db.close()


def test_same_transfer_idempotency_key_with_different_payload_is_rejected():
    _, headers = create_user()
    source = create_account(headers, name="Conflict source")
    destination = create_account(headers, name="Conflict destination")
    key = uuid4().hex

    first = client.post(
        "/transfers",
        json=transfer_payload(source["id"], destination["id"], "100000.00"),
        headers=with_key(headers, key),
    )
    assert first.status_code == 200

    conflict = client.post(
        "/transfers",
        json=transfer_payload(source["id"], destination["id"], "200000.00"),
        headers=with_key(headers, key),
    )
    assert conflict.status_code == 409


def test_transfer_audit_detects_non_zero_sum_without_polluting_database():
    user_id, headers = create_user()
    source = create_account(headers, name="Audit source")
    destination = create_account(headers, name="Audit destination")
    created = client.post(
        "/transfers",
        json=transfer_payload(source["id"], destination["id"], "50000.00"),
        headers=with_key(headers),
    )
    assert created.status_code == 200

    db = SessionLocal()
    try:
        positive_entry = (
            db.query(FinancialEventEntry)
            .filter(
                FinancialEventEntry.financial_event_id == created.json()["id"],
                FinancialEventEntry.amount > 0,
            )
            .one()
        )
        positive_entry.amount = Decimal("1.00")
        db.flush()

        divergence = find_first_transfer_divergence(db, user_id)
        assert divergence is not None
        assert divergence["reason"] == "transfer_not_zero_sum"
        db.rollback()

        clean = audit_user(db, user_id)
        assert clean["ok"] is True
        assert clean["transfer_divergence"] is None
    finally:
        db.rollback()
        db.close()
