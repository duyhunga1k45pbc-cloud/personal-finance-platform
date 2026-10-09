from __future__ import annotations

import asyncio
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Awaitable, Callable

import pytest
from alembic import command
from alembic.config import Config
from fastapi.encoders import jsonable_encoder
from sqlalchemy import create_engine, select, text, update
from sqlalchemy.engine import Engine, URL, make_url
from sqlalchemy.exc import IntegrityError, TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.canonical_service import (
    CausalDependencyError,
    ConcurrentModificationError,
)
from app.command_service import (
    IdempotencyConflictError,
    hash_command,
    normalize_idempotency_key,
)
from app.credit_card_service import validate_transaction_account_semantics
from app.models import (
    CommandReceipt,
    FinancialAccount,
    FinancialEvent,
    FinancialEventEntry,
    FinancialEventHistory,
    FinancialEventLink,
    Transaction,
    User,
)


BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROTECTED_DATABASES = {"finance_db", "finance_test_db"}
DISPOSABLE_DATABASE_PATTERN = re.compile(
    r"native_async_failure_test_[0-9a-f]{32}"
)
COMMAND_TYPE = "UPDATE_TRANSACTION"


@dataclass(frozen=True)
class DisposableDatabase:
    name: str
    sync_url: URL
    async_url: URL
    admin_engine: Engine

    def sync_engine(self, **options: Any) -> Engine:
        return create_engine(self.sync_url, **options)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _configured_postgres_url() -> URL:
    raw_url = os.getenv("MIGRATION_TEST_ADMIN_URL") or os.getenv("DATABASE_URL")
    if not raw_url:
        pytest.fail(
            "Native async experiments require DATABASE_URL or "
            "MIGRATION_TEST_ADMIN_URL to create a disposable database."
        )
    url = make_url(raw_url)
    if not url.drivername.startswith("postgresql"):
        pytest.fail("Native async experiments require PostgreSQL.")
    if url.database and url.database.lower() == "finance_db":
        pytest.fail("Refusing to derive disposable credentials from finance_db.")
    return url


@pytest.fixture
def disposable_database(
    monkeypatch: pytest.MonkeyPatch,
) -> DisposableDatabase:
    source_url = _configured_postgres_url()
    database_name = f"native_async_failure_test_{uuid.uuid4().hex}"
    if not DISPOSABLE_DATABASE_PATTERN.fullmatch(database_name):
        pytest.fail("Generated disposable database name failed validation.")
    if database_name.lower() in PROTECTED_DATABASES:
        pytest.fail("Refusing to target a protected database.")

    admin_engine = create_engine(source_url.set(database="postgres"))
    sync_url = source_url.set(
        database=database_name,
        drivername="postgresql+psycopg2",
    )
    async_url = source_url.set(
        database=database_name,
        drivername="postgresql+asyncpg",
    )
    created = False
    try:
        with admin_engine.connect().execution_options(
            isolation_level="AUTOCOMMIT"
        ) as connection:
            quoted_name = connection.dialect.identifier_preparer.quote(database_name)
            connection.exec_driver_sql(f"CREATE DATABASE {quoted_name}")
        created = True
        database_url = sync_url.render_as_string(hide_password=False)
        monkeypatch.setenv("DATABASE_URL", database_url)
        config = Config(str(BACKEND_ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
        config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
        command.upgrade(config, "head")
        database = DisposableDatabase(
            database_name, sync_url, async_url, admin_engine
        )
        yield database
    except Exception as error:
        sqlstate = getattr(getattr(error, "orig", None), "pgcode", None) or getattr(
            getattr(error, "orig", None), "sqlstate", None
        )
        if sqlstate == "42501" and not created:
            pytest.skip(
                "PostgreSQL role cannot create disposable databases; "
                "no shared database will be used."
            )
        raise
    finally:
        try:
            if created:
                with admin_engine.connect().execution_options(
                    isolation_level="AUTOCOMMIT"
                ) as connection:
                    connection.execute(
                        text(
                            """
                            SELECT pg_terminate_backend(pid)
                            FROM pg_stat_activity
                            WHERE datname = :database_name
                              AND pid <> pg_backend_pid()
                            """
                        ),
                        {"database_name": database_name},
                    )
                    quoted_name = connection.dialect.identifier_preparer.quote(
                        database_name
                    )
                    connection.exec_driver_sql(f"DROP DATABASE {quoted_name}")
        finally:
            admin_engine.dispose()


@pytest.fixture
async def async_engine(
    disposable_database: DisposableDatabase,
) -> AsyncEngine:
    engine = create_async_engine(disposable_database.async_url)
    try:
        yield engine
    finally:
        await engine.dispose()


def _seed_transaction(
    database: DisposableDatabase,
    *,
    email: str,
    description: str = "initial purchase",
) -> tuple[int, int, int]:
    from sqlalchemy.orm import Session

    from app.account_service import create_default_cash_account
    from app.canonical_service import sync_canonical_from_legacy_transaction

    with Session(database.sync_engine()) as session:
        user = User(email=email, hashed_password="test-only-not-a-real-hash")
        session.add(user)
        session.flush()
        account = create_default_cash_account(session, user.id)
        session.flush()
        transaction = Transaction(
            amount=Decimal("125.50"),
            description=description,
            category="experiment",
            date=datetime(2024, 3, 1, 12, 30),
            type="expense",
            user_id=user.id,
            account_id=account.id,
        )
        session.add(transaction)
        session.flush()
        event_row = sync_canonical_from_legacy_transaction(
            session,
            transaction,
            actor_type="USER",
            actor_user_id=user.id,
            reason="native_async_experiment_seed",
        )
        session.commit()
        return user.id, account.id, transaction.id


def _request_hash(
    *,
    transaction_id: int,
    expected_version: int,
    payload: dict[str, Any],
) -> str:
    return hash_command(
        {
            "command_type": COMMAND_TYPE,
            "transaction_id": transaction_id,
            "expected_version": expected_version,
            "payload": payload,
        }
    )


def _snapshot_event(event: FinancialEvent, entries: list[FinancialEventEntry]) -> dict:
    return {
        "event_type": event.event_type,
        "description": event.description,
        "category": event.category,
        "occurred_at": event.occurred_at.isoformat() if event.occurred_at else None,
        "effective_at": event.effective_at.isoformat() if event.effective_at else None,
        "interpretation_state": event.interpretation_state,
        "provenance": event.provenance,
        "confidence": event.confidence,
        "lifecycle_state": event.lifecycle_state,
        "version": event.version,
        "entries": [
            {
                "account_id": entry.account_id,
                "amount": format(Decimal(entry.amount), "f"),
            }
            for entry in entries
        ],
    }


async def native_async_correct_transaction(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    user_id: int,
    transaction_id: int,
    idempotency_key: str,
    expected_version: int,
    payload: dict[str, Any],
    before_commit: Callable[[AsyncSession], Awaitable[None]] | None = None,
    after_commit: Callable[[], Awaitable[None]] | None = None,
    before_version_claim: Callable[[AsyncSession], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """Test-only native-async equivalent of the legacy transaction correction."""
    key = normalize_idempotency_key(idempotency_key)
    request_digest = _request_hash(
        transaction_id=transaction_id,
        expected_version=expected_version,
        payload=payload,
    )

    async def replay_after_rollback() -> dict[str, Any] | None:
        async with session_factory() as recovery_session:
            receipt = await recovery_session.scalar(
                select(CommandReceipt).where(
                    CommandReceipt.user_id == user_id,
                    CommandReceipt.command_type == COMMAND_TYPE,
                    CommandReceipt.idempotency_key == key,
                )
            )
            if receipt is None:
                return None
            if receipt.request_hash != request_digest:
                raise IdempotencyConflictError(
                    "Idempotency key was already used for a different command payload"
                )
            return receipt.response_body

    try:
        async with session_factory() as session:
            async with session.begin():
                receipt = await session.scalar(
                    select(CommandReceipt).where(
                        CommandReceipt.user_id == user_id,
                        CommandReceipt.command_type == COMMAND_TYPE,
                        CommandReceipt.idempotency_key == key,
                    )
                )
                if receipt is not None:
                    if receipt.request_hash != request_digest:
                        raise IdempotencyConflictError(
                            "Idempotency key was already used for a different "
                            "command payload"
                        )
                    return receipt.response_body

                transaction = await session.scalar(
                    select(Transaction)
                    .join(
                        FinancialEvent,
                        FinancialEvent.legacy_transaction_id == Transaction.id,
                    )
                    .where(
                        Transaction.id == transaction_id,
                        Transaction.user_id == user_id,
                        FinancialEvent.lifecycle_state == "ACTIVE",
                    )
                )
                if transaction is None:
                    raise LookupError("Transaction not found")
                if expected_version < 1:
                    raise ValueError("Expected version must be positive")
                amount = Decimal(str(payload["amount"]))
                transaction_type = str(payload["type"])
                if amount <= 0:
                    raise ValueError("Transaction amount must be positive")
                if transaction_type not in {"income", "expense"}:
                    raise ValueError("Unsupported transaction type")

                account_id = payload.get("account_id") or transaction.account_id
                account = await session.scalar(
                    select(FinancialAccount).where(
                        FinancialAccount.id == account_id,
                        FinancialAccount.user_id == user_id,
                    )
                )
                if account is None:
                    raise ValueError("Account not found")
                validate_transaction_account_semantics(account, transaction_type)

                event_row = await session.scalar(
                    select(FinancialEvent).where(
                        FinancialEvent.legacy_transaction_id == transaction.id
                    )
                )
                if event_row is None:
                    raise ValueError("Canonical event does not exist")
                if event_row.user_id != transaction.user_id:
                    raise ValueError(
                        "Canonical event owner diverged from legacy owner"
                    )
                dependent = await session.scalar(
                    select(FinancialEventLink.id)
                    .where(FinancialEventLink.to_event_id == event_row.id)
                    .limit(1)
                )
                if dependent is not None:
                    raise CausalDependencyError(
                        "Cannot mutate an event after REFUND_OF or REVERSAL_OF "
                        "dependents exist"
                    )

                if before_version_claim is not None:
                    await before_version_claim(session)
                claimed = await session.execute(
                    update(FinancialEvent)
                    .where(
                        FinancialEvent.id == event_row.id,
                        FinancialEvent.lifecycle_state == "ACTIVE",
                        FinancialEvent.version == expected_version,
                    )
                    .values(version=FinancialEvent.version)
                    .execution_options(synchronize_session=False)
                )
                if claimed.rowcount != 1:
                    current_version = await session.scalar(
                        select(FinancialEvent.version).where(
                            FinancialEvent.id == event_row.id
                        )
                    )
                    raise ConcurrentModificationError(
                        expected_version=expected_version,
                        current_version=current_version,
                    )
                await session.refresh(event_row)

                entries = list(
                    (
                        await session.scalars(
                            select(FinancialEventEntry)
                            .where(
                                FinancialEventEntry.financial_event_id
                                == event_row.id
                            )
                            .order_by(FinancialEventEntry.id.asc())
                        )
                    ).all()
                )
                if len(entries) != 1:
                    raise ValueError(
                        "Legacy compatibility event must contain exactly one "
                        "canonical entry"
                    )
                entry = entries[0]
                description = payload.get("description")
                category = payload.get("category")
                signed_amount = amount if transaction_type == "income" else -amount
                unchanged = (
                    event_row.event_type == transaction_type.upper()
                    and event_row.description == description
                    and event_row.category == category
                    and entry.account_id == account_id
                    and Decimal(entry.amount) == signed_amount
                )
                if not unchanged:
                    previous_state = _snapshot_event(event_row, entries)
                    transaction.amount = amount
                    transaction.description = description
                    transaction.category = category
                    transaction.type = transaction_type
                    transaction.account_id = account_id
                    event_row.event_type = transaction_type.upper()
                    event_row.description = description
                    event_row.category = category
                    event_row.occurred_at = transaction.date
                    event_row.effective_at = transaction.date
                    event_row.version = expected_version + 1
                    entry.account_id = account_id
                    entry.amount = signed_amount
                    await session.flush()
                    new_state = _snapshot_event(event_row, entries)
                    session.add(
                        FinancialEventHistory(
                            financial_event_id=event_row.id,
                            user_id=user_id,
                            event_version=event_row.version,
                            transition_type="CORRECTED",
                            actor_type="USER",
                            actor_user_id=user_id,
                            previous_state=previous_state,
                            new_state=new_state,
                            reason="legacy_api_update",
                        )
                    )

                response = jsonable_encoder(
                    {
                        "id": transaction.id,
                        "amount": transaction.amount,
                        "description": transaction.description,
                        "category": transaction.category,
                        "date": transaction.date,
                        "type": transaction.type,
                        "user_id": transaction.user_id,
                        "account_id": transaction.account_id,
                        "canonical_event_id": event_row.id,
                        "canonical_version": event_row.version,
                    }
                )
                session.add(
                    CommandReceipt(
                        user_id=user_id,
                        command_type=COMMAND_TYPE,
                        idempotency_key=key,
                        request_hash=request_digest,
                        transaction_id=transaction.id,
                        response_status=200,
                        response_body=response,
                    )
                )
                await session.flush()
                if before_commit is not None:
                    await before_commit(session)
        if after_commit is not None:
            await after_commit()
        return response
    except (IntegrityError, ConcurrentModificationError):
        replay = await replay_after_rollback()
        if replay is not None:
            return replay
        raise


def _financial_state(
    database: DisposableDatabase,
    *,
    transaction_id: int,
) -> dict[str, Any]:
    engine = database.sync_engine()
    try:
        with engine.connect() as connection:
            transaction = connection.execute(
                text(
                    """
                    SELECT amount, description, category, date, type, user_id, account_id
                    FROM transactions WHERE id = :transaction_id
                    """
                ),
                {"transaction_id": transaction_id},
            ).mappings().one()
            event_row = connection.execute(
                text(
                    """
                    SELECT id, event_type, description, category, occurred_at,
                           effective_at, lifecycle_state, version
                    FROM financial_events
                    WHERE legacy_transaction_id = :transaction_id
                    """
                ),
                {"transaction_id": transaction_id},
            ).mappings().one()
            entries = connection.execute(
                text(
                    """
                    SELECT account_id, amount
                    FROM financial_event_entries
                    WHERE financial_event_id = :event_id ORDER BY id
                    """
                ),
                {"event_id": event_row["id"]},
            ).mappings().all()
            history = connection.execute(
                text(
                    """
                    SELECT event_version, transition_type, actor_type, actor_user_id,
                           previous_state, new_state, reason
                    FROM financial_event_history
                    WHERE financial_event_id = :event_id ORDER BY event_version
                    """
                ),
                {"event_id": event_row["id"]},
            ).mappings().all()
            receipts = connection.execute(
                text(
                    """
                    SELECT idempotency_key, request_hash, response_status, response_body
                    FROM command_receipts
                    WHERE transaction_id = :transaction_id
                    ORDER BY id
                    """
                ),
                {"transaction_id": transaction_id},
            ).mappings().all()
            return {
                "transaction": dict(transaction),
                "event": dict(event_row),
                "entries": [dict(row) for row in entries],
                "history": [dict(row) for row in history],
                "receipts": [dict(row) for row in receipts],
            }
    finally:
        engine.dispose()


def _pool_checked_out(engine: AsyncEngine) -> int:
    pool = engine.sync_engine.pool
    return int(getattr(pool, "checkedout")())


def _new_session_factory(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.mark.anyio
async def test_native_async_correction_preserves_idempotency_and_financial_invariants(
    disposable_database: DisposableDatabase,
    async_engine: AsyncEngine,
) -> None:
    user_id, account_id, transaction_id = _seed_transaction(
        disposable_database,
        email=f"native-baseline-{uuid.uuid4().hex}@example.invalid",
    )
    factory = _new_session_factory(async_engine)
    payload = {
        "amount": Decimal("180.75"),
        "description": "corrected purchase",
        "category": "food",
        "type": "expense",
        "account_id": account_id,
    }
    request_args = {
        "user_id": user_id,
        "transaction_id": transaction_id,
        "idempotency_key": "native-correction-1",
        "expected_version": 1,
        "payload": payload,
    }
    result = await native_async_correct_transaction(factory, **request_args)
    assert result == {
        "id": transaction_id,
        "amount": 180.75,
        "description": "corrected purchase",
        "category": "food",
        "date": "2024-03-01T12:30:00",
        "type": "expense",
        "user_id": user_id,
        "account_id": account_id,
        "canonical_event_id": result["canonical_event_id"],
        "canonical_version": 2,
    }

    replay = await native_async_correct_transaction(factory, **request_args)
    assert replay == result
    with pytest.raises(IdempotencyConflictError):
        await native_async_correct_transaction(
            factory,
            **{
                **request_args,
                "payload": {**payload, "description": "different request"},
            },
        )

    state = _financial_state(disposable_database, transaction_id=transaction_id)
    assert state["transaction"]["amount"] == Decimal("180.75")
    assert state["transaction"]["description"] == "corrected purchase"
    assert state["transaction"]["category"] == "food"
    assert state["transaction"]["type"] == "expense"
    assert state["transaction"]["account_id"] == account_id
    assert state["transaction"]["date"] == datetime(2024, 3, 1, 12, 30)
    assert state["event"]["event_type"] == "EXPENSE"
    assert state["event"]["occurred_at"] == state["transaction"]["date"]
    assert state["event"]["effective_at"] == state["transaction"]["date"]
    assert state["event"]["version"] == 2
    assert state["entries"] == [{"account_id": account_id, "amount": Decimal("-180.75")}]
    assert [row["event_version"] for row in state["history"]] == [1, 2]
    assert state["history"][1]["transition_type"] == "CORRECTED"
    assert state["history"][1]["actor_type"] == "USER"
    assert state["history"][1]["actor_user_id"] == user_id
    assert state["history"][1]["reason"] == "legacy_api_update"
    assert len(state["receipts"]) == 1
    assert state["receipts"][0]["response_body"] == result


@pytest.mark.anyio
async def test_native_async_cancellation_and_precommit_failure_rollback(
    disposable_database: DisposableDatabase,
    async_engine: AsyncEngine,
) -> None:
    user_id, _, transaction_id = _seed_transaction(
        disposable_database,
        email=f"native-cancel-{uuid.uuid4().hex}@example.invalid",
    )
    factory = _new_session_factory(async_engine)
    payload = {
        "amount": Decimal("250.00"),
        "description": "must roll back",
        "category": "test",
        "type": "expense",
        "account_id": None,
    }
    original = _financial_state(disposable_database, transaction_id=transaction_id)

    entered_gate = asyncio.Event()
    never_release = asyncio.Event()

    async def hold_before_commit(session: AsyncSession) -> None:
        entered_gate.set()
        await never_release.wait()

    task = asyncio.create_task(
        native_async_correct_transaction(
            factory,
            user_id=user_id,
            transaction_id=transaction_id,
            idempotency_key="cancel-before-commit",
            expected_version=1,
            payload=payload,
            before_commit=hold_before_commit,
        )
    )
    await asyncio.wait_for(entered_gate.wait(), timeout=10)
    assert _pool_checked_out(async_engine) == 1
    assert _financial_state(disposable_database, transaction_id=transaction_id) == original
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=10)
    assert _pool_checked_out(async_engine) == 0
    assert _financial_state(disposable_database, transaction_id=transaction_id) == original

    reached_precommit = asyncio.Event()

    class InjectedPrecommitFailure(RuntimeError):
        pass

    async def fail_before_commit(session: AsyncSession) -> None:
        reached_precommit.set()
        raise InjectedPrecommitFailure("injected after flush, before COMMIT")

    failing_task = asyncio.create_task(
        native_async_correct_transaction(
            factory,
            user_id=user_id,
            transaction_id=transaction_id,
            idempotency_key="failure-before-commit",
            expected_version=1,
            payload=payload,
            before_commit=fail_before_commit,
        )
    )
    await asyncio.wait_for(reached_precommit.wait(), timeout=10)
    with pytest.raises(InjectedPrecommitFailure):
        await asyncio.wait_for(failing_task, timeout=10)
    assert _pool_checked_out(async_engine) == 0
    assert _financial_state(disposable_database, transaction_id=transaction_id) == original


@pytest.mark.anyio
async def test_native_async_postcommit_response_loss_replays_durable_receipt(
    disposable_database: DisposableDatabase,
    async_engine: AsyncEngine,
) -> None:
    user_id, account_id, transaction_id = _seed_transaction(
        disposable_database,
        email=f"native-response-loss-{uuid.uuid4().hex}@example.invalid",
    )
    factory = _new_session_factory(async_engine)
    payload = {
        "amount": Decimal("90.00"),
        "description": "committed, response lost",
        "category": "utilities",
        "type": "expense",
        "account_id": account_id,
    }

    class SimulatedResponseLoss(RuntimeError):
        pass

    async def lose_response_after_commit() -> None:
        raise SimulatedResponseLoss("synthetic exception after COMMIT returned")

    with pytest.raises(SimulatedResponseLoss):
        await native_async_correct_transaction(
            factory,
            user_id=user_id,
            transaction_id=transaction_id,
            idempotency_key="postcommit-response-loss",
            expected_version=1,
            payload=payload,
            after_commit=lose_response_after_commit,
        )

    state = _financial_state(disposable_database, transaction_id=transaction_id)
    assert state["transaction"]["amount"] == Decimal("90.00")
    assert state["event"]["version"] == 2
    assert state["entries"] == [{"account_id": account_id, "amount": Decimal("-90.00")}]
    assert len(state["history"]) == 2
    assert len(state["receipts"]) == 1
    replay = await native_async_correct_transaction(
        factory,
        user_id=user_id,
        transaction_id=transaction_id,
        idempotency_key="postcommit-response-loss",
        expected_version=1,
        payload=payload,
    )
    assert replay == state["receipts"][0]["response_body"]
    assert _pool_checked_out(async_engine) == 0
    replayed_state = _financial_state(disposable_database, transaction_id=transaction_id)
    assert len(replayed_state["history"]) == 2
    assert len(replayed_state["receipts"]) == 1


@pytest.mark.anyio
async def test_native_async_concurrent_writers_prove_postgres_lock_contention(
    disposable_database: DisposableDatabase,
    async_engine: AsyncEngine,
) -> None:
    user_id, account_id, transaction_id = _seed_transaction(
        disposable_database,
        email=f"native-race-{uuid.uuid4().hex}@example.invalid",
    )
    factory = _new_session_factory(async_engine)
    writer_a_staged = asyncio.Event()
    release_writer_a = asyncio.Event()
    writer_b_at_claim = asyncio.Event()
    backend_pids: dict[str, int] = {}

    async def capture_writer_b_pid(session: AsyncSession) -> None:
        backend_pids["b"] = int(
            await session.scalar(text("SELECT pg_backend_pid()"))
        )
        writer_b_at_claim.set()

    async def hold_writer_a(session: AsyncSession) -> None:
        backend_pids["a"] = int(
            await session.scalar(text("SELECT pg_backend_pid()"))
        )
        writer_a_staged.set()
        await release_writer_a.wait()

    async def writer_b_claim_gate(session: AsyncSession) -> None:
        await capture_writer_b_pid(session)

    def payload(description: str) -> dict[str, Any]:
        return {
            "amount": Decimal("160.00"),
            "description": description,
            "category": "race",
            "type": "expense",
            "account_id": account_id,
        }

    writer_a = asyncio.create_task(
        native_async_correct_transaction(
            factory,
            user_id=user_id,
            transaction_id=transaction_id,
            idempotency_key="race-writer-a",
            expected_version=1,
            payload=payload("winner"),
            before_commit=hold_writer_a,
        )
    )
    await asyncio.wait_for(writer_a_staged.wait(), timeout=10)
    writer_b = asyncio.create_task(
        native_async_correct_transaction(
            factory,
            user_id=user_id,
            transaction_id=transaction_id,
            idempotency_key="race-writer-b",
            expected_version=1,
            payload=payload("loser"),
            before_version_claim=writer_b_claim_gate,
        )
    )
    await asyncio.wait_for(writer_b_at_claim.wait(), timeout=10)
    assert backend_pids["a"] != backend_pids["b"]

    observer = disposable_database.sync_engine()
    try:
        deadline = asyncio.get_running_loop().time() + 10
        observed_lock_wait = False
        while asyncio.get_running_loop().time() < deadline:
            with observer.connect() as connection:
                observed_lock_wait = connection.execute(
                    text(
                        """
                        SELECT wait_event_type = 'Lock'
                        FROM pg_stat_activity
                        WHERE pid = :pid
                        """
                    ),
                    {"pid": backend_pids["b"]},
                ).scalar_one_or_none() is True
            if observed_lock_wait:
                break
            await asyncio.sleep(0.02)
        assert observed_lock_wait, (
            "Writer B never appeared in pg_stat_activity waiting on a PostgreSQL lock"
        )
    finally:
        observer.dispose()

    release_writer_a.set()
    winner = await asyncio.wait_for(writer_a, timeout=10)
    with pytest.raises(ConcurrentModificationError) as stale:
        await asyncio.wait_for(writer_b, timeout=10)
    assert stale.value.expected_version == 1
    assert stale.value.current_version == 2
    assert winner["description"] == "winner"

    state = _financial_state(disposable_database, transaction_id=transaction_id)
    assert state["transaction"]["description"] == "winner"
    assert state["event"]["description"] == "winner"
    assert state["event"]["version"] == 2
    assert state["entries"] == [{"account_id": account_id, "amount": Decimal("-160.00")}]
    assert [row["event_version"] for row in state["history"]] == [1, 2]
    assert [row["idempotency_key"] for row in state["receipts"]] == [
        "race-writer-a"
    ]
    assert _pool_checked_out(async_engine) == 0


@pytest.mark.anyio
async def test_native_async_pool_exhaustion_times_out_and_recovers(
    disposable_database: DisposableDatabase,
) -> None:
    engine = create_async_engine(
        disposable_database.async_url,
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.1,
    )
    try:
        async with engine.connect() as held:
            with pytest.raises(SQLAlchemyTimeoutError):
                await asyncio.wait_for(engine.connect(), timeout=5)
        async with engine.connect() as recovered:
            assert await recovered.scalar(text("SELECT 1")) == 1
    finally:
        await engine.dispose()
