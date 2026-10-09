from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Event, Lock
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, text

from app import causal_service
from app.canonical_audit import audit_user
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


def _create_user_and_expense() -> tuple[int, dict[str, str], int]:
    email = f"refund-race-{uuid4().hex}@example.com"
    password = "testpassword123"
    registered = client.post(
        "/auth/register",
        json={"email": email, "password": password},
    )
    assert registered.status_code == 200
    logged_in = client.post(
        "/auth/login",
        json={"email": email, "password": password},
    )
    assert logged_in.status_code == 200
    headers = {"Authorization": f"Bearer {logged_in.json()['access_token']}"}

    account = client.post(
        "/accounts",
        json={"name": "Race test account", "account_type": "BANK", "currency": "VND"},
        headers=headers,
    )
    assert account.status_code == 200
    expense = client.post(
        "/transactions",
        json={
            "amount": "100.00",
            "description": "concurrency test expense",
            "category": "test",
            "type": "expense",
            "account_id": account.json()["id"],
        },
        headers={**headers, "Idempotency-Key": uuid4().hex},
    )
    assert expense.status_code == 200
    return (
        registered.json()["id"],
        headers,
        expense.json()["canonical_event_id"],
    )


def _postgres_lock_wait_exists() -> bool:
    with engine.connect() as connection:
        return (
            connection.execute(
                text(
                    "SELECT EXISTS ("
                    "SELECT 1 FROM pg_stat_activity "
                    "WHERE datname = current_database() "
                    "AND pid <> pg_backend_pid() "
                    "AND wait_event_type = 'Lock'"
                    ")"
                )
            ).scalar_one()
        )


def test_concurrent_refunds_cannot_exceed_original_expense(monkeypatch):
    if engine.dialect.name != "postgresql":
        pytest.skip("Refund concurrency regression requires PostgreSQL")

    with SessionLocal() as db:
        assert db.execute(text("SHOW transaction_isolation")).scalar_one() == "read committed"

    user_id, headers, original_event_id = _create_user_and_expense()
    original_remaining = causal_service.refundable_remaining
    first_eligibility_check = Event()
    second_eligibility_check = Event()
    release_first = Event()
    call_lock = Lock()
    call_count = 0

    def synchronized_remaining(db, *, user_id, original_event_id):
        nonlocal call_count
        remaining = original_remaining(
            db,
            user_id=user_id,
            original_event_id=original_event_id,
        )
        with call_lock:
            call_count += 1
            current_call = call_count
        if current_call == 1:
            first_eligibility_check.set()
            if not release_first.wait(timeout=10):
                raise TimeoutError("Timed out waiting to release first refund request")
        elif current_call == 2:
            second_eligibility_check.set()
        return remaining

    monkeypatch.setattr(causal_service, "refundable_remaining", synchronized_remaining)

    def send_refund(key: str):
        return client.post(
            "/refunds",
            json={"original_event_id": original_event_id, "amount": "60.00"},
            headers={**headers, "Idempotency-Key": key},
        )

    lock_wait_observed = False
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(send_refund, uuid4().hex)
        try:
            assert first_eligibility_check.wait(timeout=10), (
                "First refund did not reach its eligibility check"
            )
            second = executor.submit(send_refund, uuid4().hex)
            deadline = time.monotonic() + 10
            while not second_eligibility_check.is_set():
                if _postgres_lock_wait_exists():
                    lock_wait_observed = True
                    break
                if time.monotonic() >= deadline:
                    pytest.fail(
                        "Second refund neither reached eligibility nor waited on a "
                        "PostgreSQL row lock"
                    )
                second_eligibility_check.wait(timeout=0.01)
        finally:
            release_first.set()

        first_response = first.result(timeout=10)
        second_response = second.result(timeout=10)

    with SessionLocal() as db:
        refund_total = (
            db.query(func.coalesce(func.sum(FinancialEventEntry.amount), 0))
            .join(
                FinancialEvent,
                FinancialEvent.id == FinancialEventEntry.financial_event_id,
            )
            .join(
                FinancialEventLink,
                FinancialEventLink.from_event_id == FinancialEvent.id,
            )
            .filter(
                FinancialEvent.user_id == user_id,
                FinancialEvent.event_type == "REFUND",
                FinancialEvent.lifecycle_state == "ACTIVE",
                FinancialEventLink.to_event_id == original_event_id,
                FinancialEventLink.relation_type == "REFUND_OF",
            )
            .scalar()
        )
        assert Decimal(refund_total) <= Decimal("100.00"), (
            f"Active refunds committed as {Decimal(refund_total):.2f} "
            "against an original expense of 100.00"
        )

        assert sorted(
            [first_response.status_code, second_response.status_code]
        ) == [200, 422]
        assert first_response.status_code == 200
        assert second_response.status_code in {200, 422}
        refunds = (
            db.query(FinancialEvent)
            .join(
                FinancialEventLink,
                FinancialEventLink.from_event_id == FinancialEvent.id,
            )
            .filter(
                FinancialEvent.user_id == user_id,
                FinancialEvent.event_type == "REFUND",
                FinancialEvent.lifecycle_state == "ACTIVE",
                FinancialEventLink.to_event_id == original_event_id,
                FinancialEventLink.relation_type == "REFUND_OF",
            )
            .all()
        )
        assert len(refunds) == 1
        for refund in refunds:
            link = (
                db.query(FinancialEventLink)
                .filter(FinancialEventLink.from_event_id == refund.id)
                .one()
            )
            assert link.to_event_id == original_event_id
            assert link.relation_type == "REFUND_OF"
            assert (
                db.query(FinancialEventHistory)
                .filter(
                    FinancialEventHistory.financial_event_id == refund.id,
                    FinancialEventHistory.transition_type == "CREATED",
                )
                .count()
                == 1
            )
        assert audit_user(db, user_id)["causal_divergence"] is None

    assert lock_wait_observed


def test_concurrent_same_key_refund_replays_winning_command(monkeypatch):
    if engine.dialect.name != "postgresql":
        pytest.skip("Refund idempotency concurrency regression requires PostgreSQL")

    user_id, headers, original_event_id = _create_user_and_expense()
    original_remaining = causal_service.refundable_remaining
    first_eligibility_check = Event()
    second_eligibility_check = Event()
    release_first = Event()
    call_lock = Lock()
    call_count = 0

    def synchronized_remaining(db, *, user_id, original_event_id):
        nonlocal call_count
        remaining = original_remaining(
            db,
            user_id=user_id,
            original_event_id=original_event_id,
        )
        with call_lock:
            call_count += 1
            current_call = call_count
        if current_call == 1:
            first_eligibility_check.set()
            if not release_first.wait(timeout=10):
                raise TimeoutError("Timed out waiting to release first refund request")
        elif current_call == 2:
            second_eligibility_check.set()
        return remaining

    monkeypatch.setattr(causal_service, "refundable_remaining", synchronized_remaining)
    key = uuid4().hex

    def send_refund():
        return client.post(
            "/refunds",
            json={"original_event_id": original_event_id, "amount": "60.00"},
            headers={**headers, "Idempotency-Key": key},
        )

    lock_wait_observed = False
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(send_refund)
        try:
            assert first_eligibility_check.wait(timeout=10), (
                "First refund did not reach its eligibility check"
            )
            second = executor.submit(send_refund)
            deadline = time.monotonic() + 10
            while not second_eligibility_check.is_set():
                if _postgres_lock_wait_exists():
                    lock_wait_observed = True
                    break
                if time.monotonic() >= deadline:
                    pytest.fail(
                        "Duplicate refund neither reached eligibility nor waited on a "
                        "PostgreSQL row lock"
                    )
                second_eligibility_check.wait(timeout=0.01)
        finally:
            release_first.set()

        first_response = first.result(timeout=10)
        second_response = second.result(timeout=10)

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert second_response.json() == first_response.json()
    with SessionLocal() as db:
        refund_total = (
            db.query(func.coalesce(func.sum(FinancialEventEntry.amount), 0))
            .join(
                FinancialEvent,
                FinancialEvent.id == FinancialEventEntry.financial_event_id,
            )
            .join(
                FinancialEventLink,
                FinancialEventLink.from_event_id == FinancialEvent.id,
            )
            .filter(
                FinancialEvent.user_id == user_id,
                FinancialEvent.event_type == "REFUND",
                FinancialEvent.lifecycle_state == "ACTIVE",
                FinancialEventLink.to_event_id == original_event_id,
                FinancialEventLink.relation_type == "REFUND_OF",
            )
            .scalar()
        )
        assert Decimal(refund_total) == Decimal("60.00")
        assert (
            db.query(FinancialEvent)
            .join(
                FinancialEventLink,
                FinancialEventLink.from_event_id == FinancialEvent.id,
            )
            .filter(
                FinancialEvent.user_id == user_id,
                FinancialEvent.event_type == "REFUND",
                FinancialEventLink.to_event_id == original_event_id,
            )
            .count()
            == 1
        )
        assert (
            db.query(CommandReceipt)
            .filter(
                CommandReceipt.user_id == user_id,
                CommandReceipt.command_type == "CREATE_REFUND",
                CommandReceipt.idempotency_key == key,
            )
            .count()
            == 1
        )
        assert audit_user(db, user_id)["causal_divergence"] is None
    assert lock_wait_observed


def test_concurrent_refund_and_reversal_remain_mutually_exclusive(monkeypatch):
    if engine.dialect.name != "postgresql":
        pytest.skip("Causal-command concurrency regression requires PostgreSQL")

    user_id, headers, original_event_id = _create_user_and_expense()
    original_validation = causal_service._validate_original_has_no_reversal
    first_validation_check = Event()
    second_validation_check = Event()
    release_first = Event()
    call_lock = Lock()
    call_count = 0

    def synchronized_validation(db, event_id):
        nonlocal call_count
        original_validation(db, event_id)
        if event_id != original_event_id:
            return
        with call_lock:
            call_count += 1
            current_call = call_count
        if current_call == 1:
            first_validation_check.set()
            if not release_first.wait(timeout=10):
                raise TimeoutError("Timed out waiting to release first causal command")
        elif current_call == 2:
            second_validation_check.set()

    monkeypatch.setattr(
        causal_service, "_validate_original_has_no_reversal", synchronized_validation
    )

    def send_refund():
        return client.post(
            "/refunds",
            json={"original_event_id": original_event_id, "amount": "60.00"},
            headers={**headers, "Idempotency-Key": uuid4().hex},
        )

    def send_reversal():
        return client.post(
            "/reversals",
            json={"original_event_id": original_event_id},
            headers={**headers, "Idempotency-Key": uuid4().hex},
        )

    lock_wait_observed = False
    with ThreadPoolExecutor(max_workers=2) as executor:
        refund = executor.submit(send_refund)
        try:
            assert first_validation_check.wait(timeout=10), (
                "First causal command did not reach its eligibility check"
            )
            reversal = executor.submit(send_reversal)
            deadline = time.monotonic() + 10
            while not second_validation_check.is_set():
                if _postgres_lock_wait_exists():
                    lock_wait_observed = True
                    break
                if time.monotonic() >= deadline:
                    pytest.fail(
                        "Second causal command neither reached eligibility nor waited "
                        "on a PostgreSQL row lock"
                    )
                second_validation_check.wait(timeout=0.01)
        finally:
            release_first.set()

        refund_response = refund.result(timeout=10)
        reversal_response = reversal.result(timeout=10)

    assert refund_response.status_code == 200
    assert reversal_response.status_code == 422
    with SessionLocal() as db:
        children = (
            db.query(FinancialEvent, FinancialEventLink)
            .join(
                FinancialEventLink,
                FinancialEventLink.from_event_id == FinancialEvent.id,
            )
            .filter(
                FinancialEvent.user_id == user_id,
                FinancialEvent.lifecycle_state == "ACTIVE",
                FinancialEventLink.to_event_id == original_event_id,
            )
            .all()
        )
        assert len(children) == 1
        assert children[0][1].relation_type == "REFUND_OF"
        assert audit_user(db, user_id)["causal_divergence"] is None
    assert lock_wait_observed
