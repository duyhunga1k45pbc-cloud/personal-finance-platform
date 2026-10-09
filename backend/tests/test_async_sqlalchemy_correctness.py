from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import AsyncIterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine, URL, make_url
from sqlalchemy.exc import DBAPIError, TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from starlette.responses import JSONResponse


BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROTECTED_DATABASES = {"finance_db", "finance_test_db"}
DISPOSABLE_DATABASE_PATTERN = re.compile(
    r"async_correctness_test_[0-9a-f]{32}"
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@dataclass
class DisposableDatabase:
    name: str
    sync_url: URL
    async_url: URL
    admin_engine: Engine

    def sync_engine(self, **options) -> Engine:
        return create_engine(self.sync_url, **options)

    @asynccontextmanager
    async def async_engine(self, **options) -> AsyncIterator[AsyncEngine]:
        engine = create_async_engine(self.async_url, **options)
        try:
            yield engine
        finally:
            await engine.dispose()


def _configured_postgres_url() -> URL:
    raw_url = os.getenv("MIGRATION_TEST_ADMIN_URL") or os.getenv("DATABASE_URL")
    if not raw_url:
        pytest.fail(
            "Async SQLAlchemy tests require DATABASE_URL or "
            "MIGRATION_TEST_ADMIN_URL for disposable database creation."
        )

    url = make_url(raw_url)
    if not url.drivername.startswith("postgresql"):
        pytest.fail("Async SQLAlchemy tests require PostgreSQL.")
    if url.database and url.database.lower() == "finance_db":
        pytest.fail("Refusing to derive disposable credentials from finance_db.")
    return url


@pytest.fixture
def disposable_database(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[DisposableDatabase]:
    source_url = _configured_postgres_url()
    database_name = f"async_correctness_test_{uuid.uuid4().hex}"
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
    except DBAPIError as error:
        sqlstate = getattr(error.orig, "pgcode", None) or getattr(
            error.orig, "sqlstate", None
        )
        if sqlstate == "42501":
            admin_engine.dispose()
            pytest.skip(
                "BLOCKED: PostgreSQL role cannot create disposable databases; "
                "no shared database will be used."
            )
        admin_engine.dispose()
        raise

    database = DisposableDatabase(
        name=database_name,
        sync_url=sync_url,
        async_url=async_url,
        admin_engine=admin_engine,
    )
    try:
        database_url = sync_url.render_as_string(hide_password=False)
        monkeypatch.setenv("DATABASE_URL", database_url)
        config = Config(str(BACKEND_ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
        config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
        command.upgrade(config, "head")
        yield database
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


def _seed_user_and_account(sync_session, *, email: str) -> tuple[int, int]:
    from app.account_service import create_default_cash_account
    from app.models import User

    user = User(email=email, hashed_password="test-only-not-a-real-hash")
    sync_session.add(user)
    sync_session.flush()
    account = create_default_cash_account(sync_session, user.id)
    sync_session.flush()
    return user.id, account.id


def _create_transaction_bundle(
    sync_session,
    *,
    user_id: int,
    account_id: int,
    description: str,
    amount: Decimal = Decimal("125.50"),
) -> tuple[int, int]:
    from app.canonical_service import sync_canonical_from_legacy_transaction
    from app.models import Transaction

    transaction = Transaction(
        amount=amount,
        description=description,
        category="async-test",
        type="expense",
        user_id=user_id,
        account_id=account_id,
    )
    sync_session.add(transaction)
    sync_session.flush()
    event_row = sync_canonical_from_legacy_transaction(
        sync_session,
        transaction,
        actor_type="USER",
        actor_user_id=user_id,
        reason="async_sqlalchemy_test",
    )
    return transaction.id, event_row.id


async def _seed_identity(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    email: str,
) -> tuple[int, int]:
    async with session_factory.begin() as session:
        return await session.run_sync(
            lambda sync_session: _seed_user_and_account(
                sync_session,
                email=email,
            )
        )


def _count_user_financial_rows(sync_engine: Engine, user_id: int) -> dict[str, int]:
    with sync_engine.connect() as connection:
        return {
            table: connection.execute(
                text(f"SELECT count(*) FROM {table} WHERE user_id = :user_id"),
                {"user_id": user_id},
            ).scalar_one()
            for table in ("transactions", "financial_events")
        } | {
            "financial_event_entries": connection.execute(
                text(
                    """
                    SELECT count(*)
                    FROM financial_event_entries fee
                    JOIN financial_events fe ON fe.id = fee.financial_event_id
                    WHERE fe.user_id = :user_id
                    """
                ),
                {"user_id": user_id},
            ).scalar_one(),
            "financial_event_history": connection.execute(
                text(
                    """
                    SELECT count(*)
                    FROM financial_event_history feh
                    WHERE feh.user_id = :user_id
                    """
                ),
                {"user_id": user_id},
            ).scalar_one(),
            "command_receipts": connection.execute(
                text(
                    "SELECT count(*) FROM command_receipts WHERE user_id = :user_id"
                ),
                {"user_id": user_id},
            ).scalar_one(),
        }


@pytest.mark.anyio
async def test_cancellation_before_commit_rolls_back_application_financial_bundle(
    disposable_database: DisposableDatabase,
) -> None:
    from app.models import FinancialEvent, FinancialEventEntry, FinancialEventHistory, Transaction

    async with disposable_database.async_engine() as engine:
        session_factory = async_sessionmaker(engine)
        user_id, account_id = await _seed_identity(
            session_factory,
            email=f"cancel-{uuid.uuid4().hex}@example.invalid",
        )
        staged = asyncio.Event()
        wait_for_cancel = asyncio.Event()
        staged_ids: dict[str, int] = {}

        async def create_then_wait() -> None:
            async with session_factory() as session:
                async with session.begin():
                    transaction_id, event_id = await session.run_sync(
                        lambda sync_session: _create_transaction_bundle(
                            sync_session,
                            user_id=user_id,
                            account_id=account_id,
                            description="cancel-before-commit",
                        )
                    )
                    staged_ids.update(
                        transaction_id=transaction_id,
                        event_id=event_id,
                    )
                    staged.set()
                    await wait_for_cancel.wait()

        task = asyncio.create_task(create_then_wait())
        await asyncio.wait_for(staged.wait(), timeout=5)
        assert staged_ids["transaction_id"] > 0
        assert staged_ids["event_id"] > 0

        sync_engine = disposable_database.sync_engine()
        try:
            assert _count_user_financial_rows(sync_engine, user_id) == {
                "transactions": 0,
                "financial_events": 0,
                "financial_event_entries": 0,
                "financial_event_history": 0,
                "command_receipts": 0,
            }
        finally:
            sync_engine.dispose()

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)

        sync_engine = disposable_database.sync_engine()
        try:
            assert _count_user_financial_rows(sync_engine, user_id) == {
                "transactions": 0,
                "financial_events": 0,
                "financial_event_entries": 0,
                "financial_event_history": 0,
                "command_receipts": 0,
            }
            with sync_engine.connect() as connection:
                assert connection.execute(
                    text(
                        "SELECT count(*) FROM transactions WHERE id = :id"
                    ),
                    {"id": staged_ids["transaction_id"]},
                ).scalar_one() == 0
                assert connection.execute(
                    text(
                        "SELECT count(*) FROM financial_events WHERE id = :id"
                    ),
                    {"id": staged_ids["event_id"]},
                ).scalar_one() == 0
        finally:
            sync_engine.dispose()


@pytest.mark.anyio
async def test_simulated_lost_acknowledgment_after_commit_replays_idempotently(
    disposable_database: DisposableDatabase,
) -> None:
    from app.models import (
        CommandReceipt,
        FinancialEvent,
        FinancialEventEntry,
        FinancialEventHistory,
        Transaction,
        User,
    )
    from app.routers.transactions import create_transaction
    from app.schemas import TransactionCreate

    class SimulatedLostAcknowledgment(RuntimeError):
        pass

    async with disposable_database.async_engine() as engine:
        session_factory = async_sessionmaker(engine)
        user_id, _ = await _seed_identity(
            session_factory,
            email=f"lost-ack-{uuid.uuid4().hex}@example.invalid",
        )
        payload = TransactionCreate(
            amount=Decimal("987.65"),
            description="async-lost-ack",
            category="async-test",
            type="expense",
        )
        key = uuid.uuid4().hex
        failure_reached = False

        async with session_factory() as session:
            sync_session = session.sync_session

            def fail_after_commit(committed_session) -> None:
                nonlocal failure_reached
                if committed_session is sync_session and not failure_reached:
                    failure_reached = True
                    raise SimulatedLostAcknowledgment(
                        "simulated lost acknowledgment after COMMIT"
                    )

            event.listen(sync_session, "after_commit", fail_after_commit)
            try:
                with pytest.raises(
                    SimulatedLostAcknowledgment,
                    match="simulated lost acknowledgment after COMMIT",
                ):
                    await session.run_sync(
                        lambda sync_db: create_transaction(
                            payload,
                            key,
                            sync_db,
                            sync_db.get(User, user_id),
                        )
                    )
            finally:
                event.remove(sync_session, "after_commit", fail_after_commit)

        assert failure_reached is True

        sync_engine = disposable_database.sync_engine()
        try:
            with sync_engine.connect() as connection:
                receipt = connection.execute(
                    text(
                        """
                        SELECT response_body, transaction_id
                        FROM command_receipts
                        WHERE user_id = :user_id
                          AND command_type = 'CREATE_TRANSACTION'
                          AND idempotency_key = :key
                        """
                    ),
                    {"user_id": user_id, "key": key},
                ).mappings().one()
                assert receipt["transaction_id"] is not None
                assert connection.execute(
                    text(
                        """
                        SELECT count(*) FROM transactions
                        WHERE user_id = :user_id
                        """
                    ),
                    {"user_id": user_id},
                ).scalar_one() == 1
                assert connection.execute(
                    text(
                        """
                        SELECT count(*) FROM financial_events
                        WHERE user_id = :user_id
                        """
                    ),
                    {"user_id": user_id},
                ).scalar_one() == 1
                assert connection.execute(
                    text(
                        """
                        SELECT count(*)
                        FROM financial_event_entries fee
                        JOIN financial_events fe ON fe.id = fee.financial_event_id
                        WHERE fe.user_id = :user_id
                        """
                    ),
                    {"user_id": user_id},
                ).scalar_one() == 1
                assert connection.execute(
                    text(
                        """
                        SELECT count(*) FROM financial_event_history
                        WHERE user_id = :user_id
                        """
                    ),
                    {"user_id": user_id},
                ).scalar_one() == 1
                persisted_response = receipt["response_body"]
        finally:
            sync_engine.dispose()

        async with session_factory() as retry_session:
            replay = await retry_session.run_sync(
                lambda sync_db: create_transaction(
                    payload,
                    key,
                    sync_db,
                    sync_db.get(User, user_id),
                )
            )

        if isinstance(replay, JSONResponse):
            replay_body = json.loads(replay.body)
            assert replay.status_code == 200
        else:
            replay_body = replay
        assert replay_body == persisted_response

        sync_engine = disposable_database.sync_engine()
        try:
            counts = _count_user_financial_rows(sync_engine, user_id)
            assert counts == {
                "transactions": 1,
                "financial_events": 1,
                "financial_event_entries": 1,
                "financial_event_history": 1,
                "command_receipts": 1,
            }
        finally:
            sync_engine.dispose()


@pytest.mark.anyio
async def test_concurrent_async_sessions_preserve_expected_version_guard(
    disposable_database: DisposableDatabase,
) -> None:
    from app.canonical_service import (
        ConcurrentModificationError,
        correct_legacy_transaction_with_expected_version,
    )
    from app.models import (
        FinancialEvent,
        FinancialEventEntry,
        FinancialEventHistory,
        Transaction,
    )

    async with disposable_database.async_engine(pool_size=5) as engine:
        session_factory = async_sessionmaker(engine)
        user_id, account_id = await _seed_identity(
            session_factory,
            email=f"guarded-{uuid.uuid4().hex}@example.invalid",
        )
        async with session_factory.begin() as setup_session:
            transaction_id, event_id = await setup_session.run_sync(
                lambda sync_session: _create_transaction_bundle(
                    sync_session,
                    user_id=user_id,
                    account_id=account_id,
                    description="guarded-base",
                )
            )

        writer_a_claimed = asyncio.Event()
        allow_writer_a_commit = asyncio.Event()
        writer_b_attempting = asyncio.Event()
        writer_pids: dict[str, int] = {}

        def correction(sync_session, *, description: str, amount: str) -> int:
            transaction = sync_session.query(Transaction).filter(
                Transaction.id == transaction_id,
                Transaction.user_id == user_id,
            ).one()
            event_row = correct_legacy_transaction_with_expected_version(
                sync_session,
                transaction,
                amount=Decimal(amount),
                description=description,
                category="async-test",
                transaction_type="expense",
                account_id=account_id,
                expected_version=1,
                actor_type="USER",
                actor_user_id=user_id,
                reason="async_guarded_write",
            )
            return event_row.version

        async def writer_a() -> tuple[str, int]:
            async with session_factory() as session:
                async with session.begin():
                    backend_pid = await session.scalar(text("SELECT pg_backend_pid()"))
                    writer_pids["a"] = int(backend_pid)
                    version = await session.run_sync(
                        lambda sync_session: correction(
                            sync_session,
                            description="async-writer-a",
                            amount="200.00",
                        )
                    )
                    writer_a_claimed.set()
                    await allow_writer_a_commit.wait()
                return "ok", version

        async def writer_b() -> tuple[str, int | None]:
            await asyncio.wait_for(writer_a_claimed.wait(), timeout=5)
            async with session_factory() as session:
                try:
                    async with session.begin():
                        backend_pid = await session.scalar(
                            text("SELECT pg_backend_pid()")
                        )
                        writer_pids["b"] = int(backend_pid)
                        writer_b_attempting.set()
                        version = await session.run_sync(
                            lambda sync_session: correction(
                                sync_session,
                                description="async-writer-b",
                                amount="300.00",
                            )
                        )
                    return "ok", version
                except ConcurrentModificationError as error:
                    return "conflict", error.current_version

        task_a = asyncio.create_task(writer_a())
        task_b = asyncio.create_task(writer_b())
        sync_engine = disposable_database.sync_engine()
        try:
            await asyncio.wait_for(writer_a_claimed.wait(), timeout=5)
            await asyncio.wait_for(writer_b_attempting.wait(), timeout=5)
            assert writer_pids["a"] != writer_pids["b"]

            deadline = time.monotonic() + 8
            last_activity = None
            while time.monotonic() < deadline:
                with sync_engine.connect() as connection:
                    last_activity = connection.execute(
                        text(
                            """
                            SELECT state, wait_event_type, wait_event, query
                            FROM pg_stat_activity
                            WHERE pid = :pid
                            """
                        ),
                        {"pid": writer_pids["b"]},
                    ).mappings().one_or_none()
                if (
                    last_activity is not None
                    and last_activity["wait_event_type"] == "Lock"
                    and "update financial_events set version=financial_events.version"
                    in " ".join(last_activity["query"].lower().split())
                ):
                    break
                await asyncio.sleep(0.025)
            else:
                pytest.fail(
                    "Writer B did not reach PostgreSQL lock contention on the "
                    "guarded financial_events UPDATE; "
                    f"last activity={last_activity!r}"
                )

            assert not task_b.done()
            allow_writer_a_commit.set()
            result_a, result_b = await asyncio.wait_for(
                asyncio.gather(task_a, task_b),
                timeout=8,
            )
        finally:
            allow_writer_a_commit.set()
            if not task_a.done():
                task_a.cancel()
            if not task_b.done():
                task_b.cancel()
            await asyncio.gather(task_a, task_b, return_exceptions=True)
            sync_engine.dispose()

        assert result_a == ("ok", 2)
        assert result_b == ("conflict", 2)

        async with session_factory() as verification_session:
            event_row = await verification_session.get(FinancialEvent, event_id)
            transaction = await verification_session.get(Transaction, transaction_id)
            entries = (
                await verification_session.execute(
                    text(
                        """
                        SELECT id, financial_event_id, account_id, amount
                        FROM financial_event_entries
                        WHERE financial_event_id = :event_id
                        """
                    ),
                    {"event_id": event_id},
                )
            ).mappings().all()
            history = (
                await verification_session.scalars(
                    text(
                        """
                        SELECT event_version
                        FROM financial_event_history
                        WHERE financial_event_id = :event_id
                        ORDER BY event_version
                        """
                    ),
                    {"event_id": event_id},
                )
            ).all()
            assert event_row is not None and transaction is not None
            assert event_row.version == 2
            assert event_row.description == "async-writer-a"
            assert transaction.description == "async-writer-a"
            assert transaction.amount == Decimal("200.00")
            assert len(entries) == 1
            assert entries[0].account_id == account_id
            assert Decimal(entries[0].amount) == Decimal("-200.00")
            assert [row[0] for row in history] == [1, 2]


@pytest.mark.anyio
async def test_async_pool_exhaustion_and_session_close_recover_connection(
    disposable_database: DisposableDatabase,
) -> None:
    async with disposable_database.async_engine(
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.2,
    ) as engine:
        held_connection = await engine.connect()
        try:
            with pytest.raises(SQLAlchemyTimeoutError):
                await asyncio.wait_for(engine.connect(), timeout=3)
        finally:
            await held_connection.close()

        async with engine.connect() as recovered:
            assert (await recovered.execute(text("SELECT 1"))).scalar_one() == 1

        session_factory = async_sessionmaker(engine)
        session = session_factory()
        try:
            assert (await session.execute(text("SELECT 1"))).scalar_one() == 1
            assert engine.sync_engine.pool.checkedout() == 1
        finally:
            await session.close()

        assert engine.sync_engine.pool.checkedout() == 0
        async with engine.connect() as after_close:
            assert (await after_close.execute(text("SELECT 1"))).scalar_one() == 1


@pytest.mark.anyio
async def test_task_local_async_sessions_isolate_application_writes(
    disposable_database: DisposableDatabase,
) -> None:
    async with disposable_database.async_engine(pool_size=4) as engine:
        session_factory = async_sessionmaker(engine)
        user_id, account_id = await _seed_identity(
            session_factory,
            email=f"ownership-{uuid.uuid4().hex}@example.invalid",
        )
        both_staged = asyncio.Event()
        release_sessions = asyncio.Event()
        staged: dict[str, dict[str, int]] = {}
        staged_lock = asyncio.Lock()

        async def task_local_writer(
            label: str,
            *,
            should_commit: bool,
        ) -> tuple[str, dict[str, int]]:
            class IntentionalRollback(Exception):
                pass

            async with session_factory() as session:
                session_identity = id(session)
                backend_pid: int | None = None
                try:
                    async with session.begin():
                        backend_pid = int(
                            await session.scalar(text("SELECT pg_backend_pid()"))
                        )
                        transaction_id, event_id = await session.run_sync(
                            lambda sync_session: _create_transaction_bundle(
                                sync_session,
                                user_id=user_id,
                                account_id=account_id,
                                description=f"session-owner-{label}",
                            )
                        )
                        async with staged_lock:
                            staged[label] = {
                                "session_identity": session_identity,
                                "backend_pid": backend_pid,
                                "transaction_id": transaction_id,
                                "event_id": event_id,
                            }
                            if len(staged) == 2:
                                both_staged.set()
                        await release_sessions.wait()
                        if not should_commit:
                            raise IntentionalRollback()
                    return "committed", staged[label]
                except IntentionalRollback:
                    return "rolled_back", staged[label]

        task_a = asyncio.create_task(
            task_local_writer("a", should_commit=True)
        )
        task_b = asyncio.create_task(
            task_local_writer("b", should_commit=False)
        )
        sync_engine = disposable_database.sync_engine()
        try:
            await asyncio.wait_for(both_staged.wait(), timeout=5)
            assert staged["a"]["session_identity"] != staged["b"]["session_identity"]
            assert staged["a"]["backend_pid"] != staged["b"]["backend_pid"]
            with sync_engine.connect() as connection:
                assert connection.execute(
                    text(
                        """
                        SELECT count(*) FROM transactions
                        WHERE user_id = :user_id
                        """
                    ),
                    {"user_id": user_id},
                ).scalar_one() == 0
            release_sessions.set()
            results = await asyncio.wait_for(
                asyncio.gather(task_a, task_b),
                timeout=8,
            )
        finally:
            release_sessions.set()
            if not task_a.done():
                task_a.cancel()
            if not task_b.done():
                task_b.cancel()
            await asyncio.gather(task_a, task_b, return_exceptions=True)
            sync_engine.dispose()

        assert sorted(result[0] for result in results) == [
            "committed",
            "rolled_back",
        ]
        sync_engine = disposable_database.sync_engine()
        try:
            with sync_engine.connect() as connection:
                transactions = connection.execute(
                    text(
                        """
                        SELECT id, description
                        FROM transactions
                        WHERE user_id = :user_id
                        """
                    ),
                    {"user_id": user_id},
                ).mappings().all()
                assert len(transactions) == 1
                committed_description = transactions[0]["description"]
                committed_bundle = connection.execute(
                    text(
                        """
                        SELECT fe.id AS event_id, fee.id AS entry_id, feh.id AS history_id
                        FROM financial_events fe
                        JOIN financial_event_entries fee
                          ON fee.financial_event_id = fe.id
                        JOIN financial_event_history feh
                          ON feh.financial_event_id = fe.id
                        WHERE fe.legacy_transaction_id = :transaction_id
                        """
                    ),
                    {"transaction_id": transactions[0]["id"]},
                ).one()
                assert committed_bundle.event_id is not None
                assert committed_bundle.entry_id is not None
                assert committed_bundle.history_id is not None
                assert connection.execute(
                    text(
                        """
                        SELECT count(*)
                        FROM financial_events fe
                        WHERE fe.user_id = :user_id
                        """
                    ),
                    {"user_id": user_id},
                ).scalar_one() == 1
                assert connection.execute(
                    text(
                        """
                        SELECT count(*)
                        FROM financial_event_entries fee
                        JOIN financial_events fe ON fe.id = fee.financial_event_id
                        WHERE fe.user_id = :user_id
                        """
                    ),
                    {"user_id": user_id},
                ).scalar_one() == 1
                assert connection.execute(
                    text(
                        """
                        SELECT count(*)
                        FROM financial_event_history feh
                        WHERE feh.user_id = :user_id
                        """
                    ),
                    {"user_id": user_id},
                ).scalar_one() == 1
                assert committed_description in {
                    "session-owner-a",
                    "session-owner-b",
                }
        finally:
            sync_engine.dispose()
