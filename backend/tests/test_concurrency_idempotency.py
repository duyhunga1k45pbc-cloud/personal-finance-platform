from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import DBAPIError

from app.canonical_service import (
    ConcurrentModificationError,
    correct_legacy_transaction_with_expected_version,
)
from app.database import Base, SessionLocal, engine
from app.main import app
from app.models import (
    CommandReceipt,
    FinancialEvent,
    FinancialEventHistory,
    Transaction,
)

client = TestClient(app)
Base.metadata.create_all(bind=engine)


def create_user():
    email = f"task4_{uuid4().hex}@example.com"
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


def command_headers(headers, *, key=None, expected_version=None):
    result = dict(headers)
    result["Idempotency-Key"] = key or uuid4().hex
    if expected_version is not None:
        result["X-Expected-Version"] = str(expected_version)
    return result


def expense_payload(amount="50000.00", description="lunch"):
    return {
        "amount": amount,
        "description": description,
        "category": "food",
        "type": "expense",
    }


def test_create_requires_explicit_idempotency_key():
    _, headers = create_user()
    response = client.post(
        "/transactions",
        json=expense_payload(),
        headers=headers,
    )
    assert response.status_code == 422


def test_same_create_command_replays_same_response_without_duplicate_event():
    user_id, headers = create_user()
    key = uuid4().hex
    request_headers = command_headers(headers, key=key)

    first = client.post(
        "/transactions",
        json=expense_payload(),
        headers=request_headers,
    )
    second = client.post(
        "/transactions",
        json=expense_payload(),
        headers=request_headers,
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == first.json()

    transaction_id = first.json()["id"]
    db = SessionLocal()
    try:
        assert (
            db.query(Transaction)
            .filter(Transaction.id == transaction_id, Transaction.user_id == user_id)
            .count()
            == 1
        )
        assert (
            db.query(FinancialEvent)
            .filter(FinancialEvent.legacy_transaction_id == transaction_id)
            .count()
            == 1
        )
        assert (
            db.query(CommandReceipt)
            .filter(
                CommandReceipt.user_id == user_id,
                CommandReceipt.command_type == "CREATE_TRANSACTION",
                CommandReceipt.idempotency_key == key,
            )
            .count()
            == 1
        )
    finally:
        db.close()


def test_same_idempotency_key_with_different_payload_is_rejected():
    user_id, headers = create_user()
    key = uuid4().hex

    first = client.post(
        "/transactions",
        json=expense_payload("50000.00", "first"),
        headers=command_headers(headers, key=key),
    )
    assert first.status_code == 200

    conflict = client.post(
        "/transactions",
        json=expense_payload("75000.00", "different"),
        headers=command_headers(headers, key=key),
    )
    assert conflict.status_code == 409

    db = SessionLocal()
    try:
        assert db.query(Transaction).filter(Transaction.user_id == user_id).count() == 1
    finally:
        db.close()


def test_stale_update_is_rejected_without_overwriting_newer_state():
    _, headers = create_user()
    created = client.post(
        "/transactions",
        json=expense_payload("100000.00", "v1"),
        headers=command_headers(headers),
    )
    assert created.status_code == 200
    transaction_id = created.json()["id"]
    version_1 = created.json()["canonical_version"]

    writer_a = client.put(
        f"/transactions/{transaction_id}",
        json=expense_payload("200000.00", "writer-a"),
        headers=command_headers(headers, expected_version=version_1),
    )
    assert writer_a.status_code == 200
    assert writer_a.json()["canonical_version"] == 2

    stale_writer_b = client.put(
        f"/transactions/{transaction_id}",
        json=expense_payload("300000.00", "writer-b"),
        headers=command_headers(headers, expected_version=version_1),
    )
    assert stale_writer_b.status_code == 409
    assert stale_writer_b.json()["detail"]["code"] == "stale_version"
    assert stale_writer_b.json()["detail"]["current_version"] == 2

    current = client.get(f"/transactions/{transaction_id}", headers=headers)
    assert current.status_code == 200
    assert current.json()["description"] == "writer-a"
    assert Decimal(str(current.json()["amount"])) == Decimal("200000.00")
    assert current.json()["canonical_version"] == 2

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
            .order_by(FinancialEventHistory.event_version.asc())
            .all()
        )
        assert event.version == 2
        assert [row.event_version for row in history] == [1, 2]
    finally:
        db.close()


def test_update_retry_with_same_command_key_replays_success_after_version_changed():
    _, headers = create_user()
    created = client.post(
        "/transactions",
        json=expense_payload("100000.00", "v1"),
        headers=command_headers(headers),
    )
    transaction_id = created.json()["id"]
    version_1 = created.json()["canonical_version"]
    update_key = uuid4().hex
    update_headers = command_headers(
        headers,
        key=update_key,
        expected_version=version_1,
    )
    payload = expense_payload("250000.00", "v2")

    first = client.put(
        f"/transactions/{transaction_id}",
        json=payload,
        headers=update_headers,
    )
    replay = client.put(
        f"/transactions/{transaction_id}",
        json=payload,
        headers=update_headers,
    )

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert first.json()["canonical_version"] == 2

    db = SessionLocal()
    try:
        event = (
            db.query(FinancialEvent)
            .filter(FinancialEvent.legacy_transaction_id == transaction_id)
            .one()
        )
        assert event.version == 2
        assert (
            db.query(FinancialEventHistory)
            .filter(FinancialEventHistory.financial_event_id == event.id)
            .count()
            == 2
        )
    finally:
        db.close()


def test_delete_retry_replays_original_success_after_event_is_voided():
    _, headers = create_user()
    created = client.post(
        "/transactions",
        json=expense_payload(),
        headers=command_headers(headers),
    )
    transaction_id = created.json()["id"]
    delete_key = uuid4().hex
    delete_headers = command_headers(
        headers,
        key=delete_key,
        expected_version=created.json()["canonical_version"],
    )

    first = client.delete(
        f"/transactions/{transaction_id}",
        headers=delete_headers,
    )
    replay = client.delete(
        f"/transactions/{transaction_id}",
        headers=delete_headers,
    )

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert first.json()["canonical_version"] == 2
    assert client.get(f"/transactions/{transaction_id}", headers=headers).status_code == 404


def test_two_real_postgres_writers_cannot_both_claim_same_version():
    if engine.dialect.name != "postgresql":
        pytest.skip("Concurrent compare-and-swap test is PostgreSQL-specific")

    user_id, headers = create_user()
    created = client.post(
        "/transactions",
        json=expense_payload("100000.00", "base"),
        headers=command_headers(headers),
    )
    transaction_id = created.json()["id"]
    expected_version = created.json()["canonical_version"]

    barrier = Barrier(2)

    def writer(amount, description):
        db = SessionLocal()
        try:
            transaction = (
                db.query(Transaction)
                .filter(
                    Transaction.id == transaction_id,
                    Transaction.user_id == user_id,
                )
                .one()
            )
            barrier.wait(timeout=5)
            try:
                event = correct_legacy_transaction_with_expected_version(
                    db,
                    transaction,
                    amount=Decimal(amount),
                    description=description,
                    category="food",
                    transaction_type="expense",
                    account_id=transaction.account_id,
                    expected_version=expected_version,
                    actor_type="USER",
                    actor_user_id=user_id,
                    reason="postgres_concurrency_test",
                )
                db.commit()
                return ("ok", event.version)
            except ConcurrentModificationError as exc:
                db.rollback()
                return ("conflict", exc.current_version)
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda args: writer(*args),
                [
                    ("200000.00", "writer-a"),
                    ("300000.00", "writer-b"),
                ],
            )
        )

    assert sorted(result[0] for result in results) == ["conflict", "ok"]

    db = SessionLocal()
    try:
        event = (
            db.query(FinancialEvent)
            .filter(FinancialEvent.legacy_transaction_id == transaction_id)
            .one()
        )
        assert event.version == 2
        assert (
            db.query(FinancialEventHistory)
            .filter(FinancialEventHistory.financial_event_id == event.id)
            .count()
            == 2
        )
    finally:
        db.close()


def test_command_receipts_are_append_only_in_postgresql():
    if engine.dialect.name != "postgresql":
        pytest.skip("Database-level command receipt trigger is PostgreSQL-specific")

    _, headers = create_user()
    response = client.post(
        "/transactions",
        json=expense_payload(),
        headers=command_headers(headers),
    )
    assert response.status_code == 200

    db = SessionLocal()
    try:
        receipt = db.query(CommandReceipt).order_by(CommandReceipt.id.desc()).first()
        assert receipt is not None
        receipt.response_status = 201
        with pytest.raises(DBAPIError):
            db.commit()
    finally:
        db.rollback()
        db.close()
