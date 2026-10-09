from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic, sleep
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.command_service import (
    IdempotencyConflictError,
    StoredCommandResponse,
    commit_with_idempotency_race_recovery,
)
from app.canonical_service import sync_canonical_from_legacy_transaction
from app.database import SessionLocal, engine
from app.models import CommandReceipt, FinancialAccount, Transaction, User


@pytest.fixture
def command_context() -> tuple[int, int, str]:
    if engine.dialect.name != "postgresql":
        pytest.skip("Command receipt integrity recovery tests require PostgreSQL")

    unique = uuid4().hex
    email = f"command-recovery-{unique}@example.invalid"
    with SessionLocal() as session:
        user = User(
            email=email,
            hashed_password="test-only-not-a-real-hash",
        )
        session.add(user)
        session.flush()
        account = FinancialAccount(
            user_id=user.id,
            name=f"Recovery account {unique}",
            account_type="CASH",
            currency="VND",
            is_default=True,
        )
        session.add(account)
        session.flush()
        transaction = Transaction(
            amount="10.00",
            description="command recovery fixture",
            category="test",
            type="expense",
            user_id=user.id,
            account_id=account.id,
        )
        session.add(transaction)
        session.flush()
        sync_canonical_from_legacy_transaction(
            session,
            transaction,
            actor_type="USER",
            actor_user_id=user.id,
            reason="command_integrity_recovery_test",
        )
        session.commit()
        return user.id, transaction.id, email


def _receipt(
    *,
    user_id: int,
    transaction_id: int,
    key: str,
    request_hash: str,
    response_body: dict,
) -> CommandReceipt:
    return CommandReceipt(
        user_id=user_id,
        command_type="CREATE_TRANSACTION",
        idempotency_key=key,
        request_hash=request_hash,
        transaction_id=transaction_id,
        response_status=200,
        response_body=response_body,
    )


def _seed_receipt(
    *,
    user_id: int,
    transaction_id: int,
    key: str,
    request_hash: str,
    response_body: dict,
) -> None:
    with SessionLocal() as session:
        session.add(
            _receipt(
                user_id=user_id,
                transaction_id=transaction_id,
                key=key,
                request_hash=request_hash,
                response_body=response_body,
            )
        )
        session.commit()


def _wait_for_receipt_unique_lock(backend_pid: int) -> None:
    deadline = monotonic() + 10
    while monotonic() < deadline:
        with engine.connect() as connection:
            waiting = connection.execute(
                text(
                    """
                    SELECT wait_event_type = 'Lock'
                       AND query ILIKE '%INSERT INTO command_receipts%'
                    FROM pg_stat_activity
                    WHERE pid = :pid
                    """
                ),
                {"pid": backend_pid},
            ).scalar_one_or_none()
        if waiting is True:
            return
        sleep(0.02)
    pytest.fail(
        "Concurrent command receipt insert did not reach a PostgreSQL lock wait"
    )


def _run_real_receipt_race(
    *,
    user_id: int,
    transaction_id: int,
    key: str,
    winner_hash: str,
    contender_hash: str,
    winner_body: dict,
    recovery_user_id: int | None = None,
):
    winner_session = SessionLocal()
    contender_ready = Event()
    contender_pid: dict[str, int] = {}
    winner_session.add(
        _receipt(
            user_id=user_id,
            transaction_id=transaction_id,
            key=key,
            request_hash=winner_hash,
            response_body=winner_body,
        )
    )
    winner_session.flush()

    def contend():
        with SessionLocal() as session:
            contender_pid["pid"] = session.execute(
                text("SELECT pg_backend_pid()")
            ).scalar_one()
            session.add(
                _receipt(
                    user_id=user_id,
                    transaction_id=transaction_id,
                    key=key,
                    request_hash=contender_hash,
                    response_body={"writer": "contender"},
                )
            )
            contender_ready.set()
            return commit_with_idempotency_race_recovery(
                session,
                user_id=(
                    user_id if recovery_user_id is None else recovery_user_id
                ),
                command_type="CREATE_TRANSACTION",
                idempotency_key=key,
                request_hash=contender_hash,
            )

    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(contend)
    try:
        assert contender_ready.wait(timeout=10), "contender did not start"
        _wait_for_receipt_unique_lock(contender_pid["pid"])
        winner_session.commit()
        return future.result(timeout=10)
    finally:
        winner_session.rollback()
        winner_session.close()
        if not future.done():
            winner_session.rollback()
        executor.shutdown(wait=True, cancel_futures=True)


def test_matching_idempotency_constraint_race_replays_winning_receipt(
    command_context: tuple[int, int, str],
) -> None:
    user_id, transaction_id, _ = command_context
    key = uuid4().hex
    winner_body = {"id": transaction_id, "writer": "winner"}

    recovered = _run_real_receipt_race(
        user_id=user_id,
        transaction_id=transaction_id,
        key=key,
        winner_hash="a" * 64,
        contender_hash="a" * 64,
        winner_body=winner_body,
    )

    assert recovered == StoredCommandResponse(status_code=200, body=winner_body)
    with SessionLocal() as session:
        assert (
            session.query(CommandReceipt)
            .filter(
                CommandReceipt.user_id == user_id,
                CommandReceipt.command_type == "CREATE_TRANSACTION",
                CommandReceipt.idempotency_key == key,
            )
            .count()
            == 1
        )


def test_racing_same_key_with_different_payload_is_a_conflict(
    command_context: tuple[int, int, str],
) -> None:
    user_id, transaction_id, _ = command_context
    with pytest.raises(IdempotencyConflictError):
        _run_real_receipt_race(
            user_id=user_id,
            transaction_id=transaction_id,
            key=uuid4().hex,
            winner_hash="a" * 64,
            contender_hash="b" * 64,
            winner_body={"id": transaction_id, "writer": "winner"},
        )


def test_idempotency_constraint_failure_without_matching_lookup_receipt_is_reraised(
    command_context: tuple[int, int, str],
) -> None:
    user_id, transaction_id, _ = command_context
    with pytest.raises(IntegrityError) as raised:
        _run_real_receipt_race(
            user_id=user_id,
            transaction_id=transaction_id,
            key=uuid4().hex,
            winner_hash="a" * 64,
            contender_hash="a" * 64,
            winner_body={"id": transaction_id, "writer": "winner"},
            recovery_user_id=user_id + 1,
        )

    assert getattr(raised.value.orig, "pgcode", None) == "23505"
    assert raised.value.orig.diag.constraint_name == (
        "uq_command_receipts_user_command_key"
    )


def test_unrelated_integrity_error_is_not_hidden_by_matching_receipt(
    command_context: tuple[int, int, str],
) -> None:
    user_id, transaction_id, email = command_context
    key = uuid4().hex
    response_body = {"id": transaction_id, "result": "already committed"}
    _seed_receipt(
        user_id=user_id,
        transaction_id=transaction_id,
        key=key,
        request_hash="c" * 64,
        response_body=response_body,
    )

    with SessionLocal() as session:
        session.add(User(email=email, hashed_password="duplicate-email"))
        with pytest.raises(IntegrityError) as raised:
            commit_with_idempotency_race_recovery(
                session,
                user_id=user_id,
                command_type="CREATE_TRANSACTION",
                idempotency_key=key,
                request_hash="c" * 64,
            )
    assert getattr(raised.value.orig, "pgcode", None) == "23505"
    assert raised.value.orig.diag.constraint_name == "ix_users_email"


def test_unrelated_integrity_error_without_receipt_is_reraised(
    command_context: tuple[int, int, str],
) -> None:
    user_id, _, email = command_context
    with SessionLocal() as session:
        session.add(User(email=email, hashed_password="duplicate-email"))
        with pytest.raises(IntegrityError) as raised:
            commit_with_idempotency_race_recovery(
                session,
                user_id=user_id,
                command_type="CREATE_TRANSACTION",
                idempotency_key=uuid4().hex,
                request_hash="d" * 64,
            )
    assert getattr(raised.value.orig, "pgcode", None) == "23505"
    assert raised.value.orig.diag.constraint_name == "ix_users_email"
