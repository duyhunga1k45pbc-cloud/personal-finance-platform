from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine, URL, make_url
from sqlalchemy.exc import DBAPIError


BACKEND_ROOT = Path(__file__).resolve().parents[1]
INITIAL_REVISION = "454649617ef6"
TASK_1_REVISION = "8f4c2d91a601"
TASK_2_REVISION = "5c1a7e4b2d90"
TASK_3_REVISION = "9d2b6f7c3a10"
CURRENT_HEAD = "j0a8b5c2e366"
STALE_REVISION = "i9f7a4b1d255"
PROTECTED_DATABASES = {"finance_db", "finance_test_db"}


@dataclass
class DisposableDatabase:
    name: str
    url: URL
    admin_engine: Engine
    cleanup_names: tuple[str, ...] = ()

    def engine(self) -> Engine:
        return create_engine(self.url)


def _database_url_from_environment() -> URL:
    raw_url = os.environ.get("MIGRATION_TEST_ADMIN_URL") or os.environ.get(
        "DATABASE_URL"
    )
    if not raw_url:
        pytest.fail(
            "Schema-evolution tests require DATABASE_URL or "
            "MIGRATION_TEST_ADMIN_URL for disposable PostgreSQL database creation."
        )

    url = make_url(raw_url)
    if not url.drivername.startswith("postgresql"):
        pytest.fail("Schema-evolution tests require a PostgreSQL connection URL.")
    if url.database and url.database.lower() == "finance_db":
        pytest.fail(
            "Refusing to derive disposable test credentials from finance_db."
        )
    return url


@pytest.fixture
def disposable_database() -> DisposableDatabase:
    source_url = _database_url_from_environment()
    database_name = f"schema_evolution_test_{uuid.uuid4().hex}"
    if not re.fullmatch(r"schema_evolution_test_[0-9a-f]{32}", database_name):
        pytest.fail("Generated disposable database name failed validation.")
    if database_name.lower() in PROTECTED_DATABASES:
        pytest.fail("Refusing to use a protected database as a migration target.")

    admin_url = source_url.set(database="postgres")
    admin_engine = create_engine(admin_url)
    target_url = source_url.set(database=database_name)
    database = DisposableDatabase(
        name=database_name,
        url=target_url,
        admin_engine=admin_engine,
    )

    try:
        with admin_engine.connect().execution_options(
            isolation_level="AUTOCOMMIT"
        ) as connection:
            quoted_name = connection.dialect.identifier_preparer.quote(database_name)
            connection.exec_driver_sql(f"CREATE DATABASE {quoted_name}")
    except DBAPIError as error:
        admin_engine.dispose()
        sqlstate = getattr(error.orig, "pgcode", None) or getattr(
            error.orig, "sqlstate", None
        )
        if sqlstate == "42501":
            pytest.skip(
                "BLOCKED: PostgreSQL role cannot create isolated disposable "
                "databases; no shared database will be used."
            )
        raise

    try:
        yield database
    finally:
        try:
            if database.cleanup_names:
                target_engine = database.engine()
                try:
                    event_trigger, function_name, sequence_name = (
                        database.cleanup_names
                    )
                    with target_engine.connect().execution_options(
                        isolation_level="AUTOCOMMIT"
                    ) as connection:
                        connection.exec_driver_sql(
                            f'DROP EVENT TRIGGER IF EXISTS "{event_trigger}"'
                        )
                        connection.exec_driver_sql(
                            f'DROP FUNCTION IF EXISTS public."{function_name}"()'
                        )
                        connection.exec_driver_sql(
                            f'DROP SEQUENCE IF EXISTS public."{sequence_name}"'
                        )
                finally:
                    target_engine.dispose()
        finally:
            try:
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


def _upgrade(
    database: DisposableDatabase,
    revision: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = database.url.render_as_string(hide_password=False)
    monkeypatch.setenv("DATABASE_URL", database_url)

    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    command.upgrade(config, revision)


def _insert_legacy_data(database: DisposableDatabase) -> tuple[int, list[dict]]:
    rows = [
        {
            "amount": Decimal("125000.50"),
            "description": "Salary",
            "category": "Income",
            "date": datetime(2026, 1, 10, 9, 30, 0),
            "type": "income",
        },
        {
            "amount": Decimal("250.25"),
            "description": "Lunch",
            "category": "Food",
            "date": datetime(2026, 1, 11, 12, 15, 0),
            "type": "expense",
        },
    ]
    engine = database.engine()
    try:
        with engine.begin() as connection:
            user_id = connection.execute(
                text(
                    """
                    INSERT INTO users (email, hashed_password, created_at)
                    VALUES ('migration-test@example.invalid', 'not-a-real-hash', :created_at)
                    RETURNING id
                    """
                ),
                {"created_at": datetime(2026, 1, 1)},
            ).scalar_one()
            for row in rows:
                connection.execute(
                    text(
                        """
                        INSERT INTO transactions
                            (amount, description, category, date, type, user_id)
                        VALUES
                            (:amount, :description, :category, :date, :type, :user_id)
                        """
                    ),
                    {**row, "user_id": user_id},
                )
        return user_id, rows
    finally:
        engine.dispose()


def _install_task3_failure_injector(
    database: DisposableDatabase,
) -> tuple[str, str, str]:
    suffix = uuid.uuid4().hex
    event_trigger = f"test_task3_fail_{suffix}"
    function_name = f"test_task3_fail_fn_{suffix}"
    sequence_name = f"test_task3_fail_seq_{suffix}"

    engine = database.engine()
    try:
        with engine.begin() as connection:
            is_superuser = connection.execute(
                text(
                    """
                    SELECT rolsuper
                    FROM pg_roles
                    WHERE rolname = current_user
                    """
                )
            ).scalar_one()
            if not is_superuser:
                pytest.skip(
                    "BLOCKED: M1 requires PostgreSQL superuser privileges to "
                    "install a database-scoped DDL event trigger."
                )

            database.cleanup_names = (event_trigger, function_name, sequence_name)
            connection.exec_driver_sql(
                f'CREATE SEQUENCE public."{sequence_name}"'
            )
            connection.exec_driver_sql(
                f"""
                CREATE FUNCTION public."{function_name}"()
                RETURNS event_trigger
                LANGUAGE plpgsql
                AS $$
                DECLARE
                    ddl_command record;
                    event_count bigint;
                    history_count bigint;
                BEGIN
                    IF TG_TAG <> 'CREATE TRIGGER' THEN
                        RETURN;
                    END IF;

                    FOR ddl_command IN
                        SELECT * FROM pg_event_trigger_ddl_commands()
                    LOOP
                        IF ddl_command.command_tag = 'CREATE TRIGGER'
                           AND ddl_command.object_identity
                               LIKE '%%trg_financial_event_history_append_only%%' THEN
                            IF to_regclass('public.financial_event_history') IS NULL
                               OR NOT EXISTS (
                                   SELECT 1
                                   FROM information_schema.columns
                                   WHERE table_schema = 'public'
                                     AND table_name = 'financial_events'
                                     AND column_name = 'lifecycle_state'
                               ) THEN
                                RAISE EXCEPTION
                                    'M1 injector reached target trigger before Task 3 schema DDL';
                            END IF;

                            SELECT count(*) INTO event_count
                            FROM public.financial_events;
                            SELECT count(*) INTO history_count
                            FROM public.financial_event_history;
                            IF event_count = 0 OR event_count <> history_count THEN
                                RAISE EXCEPTION
                                    'M1 injector reached target trigger before Task 3 history backfill completed';
                            END IF;

                            PERFORM nextval('public."{sequence_name}"');
                            RAISE EXCEPTION
                                'injected Task 3 failure after schema and history backfill';
                        END IF;
                    END LOOP;
                END;
                $$;
                """
            )
            connection.exec_driver_sql(
                f"""
                CREATE EVENT TRIGGER "{event_trigger}"
                ON ddl_command_end
                EXECUTE FUNCTION public."{function_name}"()
                """
            )
            assert connection.execute(
                text(
                    """
                    SELECT count(*)
                    FROM pg_event_trigger
                    WHERE evtname = :event_trigger
                      AND evtenabled = 'O'
                    """
                ),
                {"event_trigger": event_trigger},
            ).scalar_one() == 1
    finally:
        engine.dispose()

    return event_trigger, function_name, sequence_name


def test_task3_failure_rolls_back_schema_backfill_and_revision(
    disposable_database: DisposableDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = disposable_database
    _upgrade(database, INITIAL_REVISION, monkeypatch)
    _insert_legacy_data(database)
    _upgrade(database, TASK_1_REVISION, monkeypatch)
    _upgrade(database, TASK_2_REVISION, monkeypatch)

    engine = database.engine()
    try:
        with engine.connect() as connection:
            before_events = connection.execute(
                text(
                    """
                    SELECT fe.id, fe.user_id, fe.event_type, fee.account_id,
                           fee.amount
                    FROM financial_events fe
                    JOIN financial_event_entries fee
                      ON fee.financial_event_id = fe.id
                    ORDER BY fe.id
                    """
                )
            ).mappings().all()
            assert len(before_events) == 2
    finally:
        engine.dispose()

    event_trigger, _, sequence_name = _install_task3_failure_injector(database)

    with pytest.raises(DBAPIError, match="injected Task 3 failure"):
        _upgrade(database, TASK_3_REVISION, monkeypatch)

    engine = database.engine()
    try:
        with engine.connect() as connection:
            injection_evidence = connection.execute(
                text(
                    f'SELECT last_value, is_called FROM public."{sequence_name}"'
                )
            ).one()
            assert injection_evidence.last_value == 1
            assert injection_evidence.is_called is True
            assert connection.execute(
                text(
                    """
                    SELECT count(*)
                    FROM pg_event_trigger
                    WHERE evtname = :event_trigger
                      AND evtenabled = 'O'
                    """
                ),
                {"event_trigger": event_trigger},
            ).scalar_one() == 1

            revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            assert revision == TASK_2_REVISION

            after_events = connection.execute(
                text(
                    """
                    SELECT fe.id, fe.user_id, fe.event_type, fee.account_id,
                           fee.amount
                    FROM financial_events fe
                    JOIN financial_event_entries fee
                      ON fee.financial_event_id = fe.id
                    ORDER BY fe.id
                    """
                )
            ).mappings().all()
            assert after_events == before_events

            schema = inspect(connection)
            assert "financial_event_history" not in schema.get_table_names()
            event_columns = {
                column["name"]
                for column in schema.get_columns("financial_events")
            }
            assert "lifecycle_state" not in event_columns
            assert connection.execute(
                text(
                    """
                    SELECT count(*)
                    FROM pg_trigger
                    WHERE tgname = 'trg_financial_event_history_append_only'
                    """
                )
            ).scalar_one() == 0
            assert connection.execute(
                text(
                    """
                    SELECT count(*)
                    FROM pg_proc
                    WHERE proname = 'reject_financial_event_history_mutation'
                    """
                )
            ).scalar_one() == 0
    finally:
        engine.dispose()


def test_current_head_readiness_rejects_a_stale_revision_stamp(
    disposable_database: DisposableDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = disposable_database
    _upgrade(database, "head", monkeypatch)

    import app.deployment as deployment

    engine = database.engine()
    revision_status = deployment.database_revision_status
    monkeypatch.setattr(
        deployment,
        "database_revision_status",
        lambda: revision_status(engine),
    )
    deployment.deployment_state.reset_for_tests()
    try:
        at_head = deployment.deployment_readiness(
            database_readiness={"ready": True}
        )
        assert at_head["ready"] is True
        assert at_head["schema"] == "ok"
        assert at_head["current_revision"] == [CURRENT_HEAD]
        assert at_head["expected_revision"] == [CURRENT_HEAD]

        with engine.begin() as connection:
            connection.execute(
                text("UPDATE alembic_version SET version_num = :revision"),
                {"revision": STALE_REVISION},
            )

        stale = deployment.deployment_readiness(
            database_readiness={"ready": True}
        )
        assert stale["ready"] is False
        assert stale["schema"] == "revision_mismatch"
        assert stale["current_revision"] == [STALE_REVISION]
        assert stale["expected_revision"] == [CURRENT_HEAD]
    finally:
        engine.dispose()
        deployment.deployment_state.reset_for_tests()


def test_task1_and_task2_preserve_legacy_transaction_meaning(
    disposable_database: DisposableDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = disposable_database
    _upgrade(database, INITIAL_REVISION, monkeypatch)
    user_id, expected_transactions = _insert_legacy_data(database)
    _upgrade(database, TASK_1_REVISION, monkeypatch)
    _upgrade(database, TASK_2_REVISION, monkeypatch)

    engine = database.engine()
    try:
        with engine.connect() as connection:
            accounts = connection.execute(
                text(
                    """
                    SELECT id, user_id, name, account_type, currency, is_default
                    FROM financial_accounts
                    WHERE user_id = :user_id
                    """
                ),
                {"user_id": user_id},
            ).mappings().all()
            assert len(accounts) == 1
            account = accounts[0]
            assert account["name"] == "Default Cash"
            assert account["account_type"] == "CASH"
            assert account["currency"] == "VND"
            assert account["is_default"] is True

            rows = connection.execute(
                text(
                    """
                    SELECT
                        t.id AS transaction_id,
                        t.user_id AS transaction_user_id,
                        t.account_id AS transaction_account_id,
                        t.amount AS transaction_amount,
                        t.description AS transaction_description,
                        t.category AS transaction_category,
                        t.date AS transaction_date,
                        t.type AS transaction_type,
                        fe.id AS event_id,
                        fe.user_id AS event_user_id,
                        fe.event_type,
                        fe.description AS event_description,
                        fe.category AS event_category,
                        fe.occurred_at,
                        fe.effective_at,
                        fee.account_id AS entry_account_id,
                        fee.amount AS entry_amount
                    FROM transactions t
                    JOIN financial_events fe
                      ON fe.legacy_transaction_id = t.id
                    JOIN financial_event_entries fee
                      ON fee.financial_event_id = fe.id
                    ORDER BY t.id
                    """
                )
            ).mappings().all()
            assert len(rows) == len(expected_transactions) == 2

            for actual, expected in zip(rows, expected_transactions, strict=True):
                assert actual["transaction_user_id"] == user_id
                assert actual["event_user_id"] == user_id
                assert actual["transaction_account_id"] == account["id"]
                assert actual["entry_account_id"] == account["id"]
                assert actual["transaction_amount"] == expected["amount"]
                assert actual["transaction_description"] == expected["description"]
                assert actual["transaction_category"] == expected["category"]
                assert actual["transaction_date"] == expected["date"]
                assert actual["event_description"] == expected["description"]
                assert actual["event_category"] == expected["category"]
                assert actual["occurred_at"] == expected["date"]
                assert actual["effective_at"] == expected["date"]
                assert actual["event_type"] == expected["type"].upper()
                expected_signed_amount = (
                    expected["amount"]
                    if expected["type"] == "income"
                    else -expected["amount"]
                )
                assert actual["entry_amount"] == expected_signed_amount

            assert connection.execute(
                text(
                    """
                    SELECT count(*)
                    FROM financial_events
                    WHERE legacy_transaction_id IS NOT NULL
                    """
                )
            ).scalar_one() == 2
            assert connection.execute(
                text("SELECT count(*) FROM financial_event_entries")
            ).scalar_one() == 2
    finally:
        engine.dispose()
