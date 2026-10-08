from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import time
from threading import Barrier, Event
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event as sqlalchemy_event, text
from sqlalchemy.exc import DBAPIError, TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.orm import sessionmaker

from app.canonical_service import (
    ConcurrentModificationError,
    correct_legacy_transaction_with_expected_version,
)
from app.database import Base, SessionLocal, engine
from app.main import app
from app.routers import transactions as transaction_router
from app.models import (
    CommandReceipt,
    FinancialEvent,
    FinancialEventEntry,
    FinancialEventHistory,
    Transaction,
)

client = TestClient(app)
Base.metadata.create_all(bind=engine)


class _LoseSuccessfulTransactionResponse:
    def __init__(self, asgi_app, verify_committed):
        self.asgi_app = asgi_app
        self.verify_committed = verify_committed
        self.lost = False

    async def __call__(self, scope, receive, send):
        if (
            scope.get("type") != "http"
            or scope.get("method") != "POST"
            or scope.get("path") != "/transactions"
        ):
            await self.asgi_app(scope, receive, send)
            return

        async def lose_success_response(message):
            if (
                message["type"] == "http.response.start"
                and message["status"] == 200
                and not self.lost
            ):
                self.verify_committed()
                self.lost = True
                raise OSError("simulated loss of committed HTTP response")
            await send(message)

        await self.asgi_app(scope, receive, lose_success_response)


@pytest.fixture
def isolated_single_connection_engine():
    if engine.dialect.name != "postgresql":
        pytest.skip("Runtime database failure tests require PostgreSQL")

    isolated_engine = create_engine(
        engine.url,
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.1,
    )
    try:
        yield isolated_engine
    finally:
        isolated_engine.dispose()


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


def _postgres_sqlstate(error):
    pending = [error]
    seen = set()
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        original = getattr(current, "orig", None)
        code = getattr(original, "pgcode", None)
        if code is not None:
            return code
        pending.extend(
            [
                original,
                getattr(current, "__cause__", None),
                getattr(current, "__context__", None),
            ]
        )
        pending.extend(getattr(current, "exceptions", ()))
    return None


def _wait_for_postgres_lock_wait(backend_pid, timeout=8):
    deadline = time.monotonic() + timeout
    last_activity = None
    while time.monotonic() < deadline:
        db = SessionLocal()
        try:
            activity = db.execute(
                text(
                    "SELECT state, wait_event_type, wait_event, query "
                    "FROM pg_stat_activity "
                    "WHERE pid = :pid"
                ),
                {"pid": backend_pid},
            ).one_or_none()
        finally:
            db.close()
        last_activity = activity
        if (
            activity is not None
            and activity.wait_event_type == "Lock"
            and activity.query.lstrip().lower().startswith("update financial_events")
        ):
            return
        time.sleep(0.025)
    pytest.fail(
        f"PostgreSQL backend {backend_pid} never waited on the canonical event UPDATE; "
        f"last activity={last_activity!r}"
    )


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


@pytest.mark.skipif(
    engine.dialect.name != "postgresql",
    reason="Committed response-loss test requires PostgreSQL",
)
def test_retry_replays_command_after_committed_http_response_is_lost():
    user_id, headers = create_user()
    key = uuid4().hex
    payload = expense_payload("83500.00", "response-loss")
    persisted = {}

    def verify_commit_before_response_loss():
        db = SessionLocal()
        try:
            receipt = (
                db.query(CommandReceipt)
                .filter(
                    CommandReceipt.user_id == user_id,
                    CommandReceipt.command_type == "CREATE_TRANSACTION",
                    CommandReceipt.idempotency_key == key,
                )
                .one()
            )
            transaction = (
                db.query(Transaction)
                .filter(
                    Transaction.user_id == user_id,
                    Transaction.id == receipt.transaction_id,
                )
                .one()
            )
            event = (
                db.query(FinancialEvent)
                .filter(FinancialEvent.legacy_transaction_id == transaction.id)
                .one()
            )
            assert transaction.amount == Decimal("83500.00")
            assert event.version == 1
            assert receipt.response_body["id"] == transaction.id
            persisted["response_body"] = receipt.response_body
        finally:
            db.close()

    loss_app = _LoseSuccessfulTransactionResponse(
        app,
        verify_commit_before_response_loss,
    )
    with TestClient(loss_app) as lossy_client:
        with pytest.raises(OSError, match="simulated loss"):
            lossy_client.post(
                "/transactions",
                json=payload,
                headers=command_headers(headers, key=key),
            )

    assert loss_app.lost is True
    assert "response_body" in persisted

    retry = client.post(
        "/transactions",
        json=payload,
        headers=command_headers(headers, key=key),
    )

    assert retry.status_code == 200
    assert retry.json() == persisted["response_body"]

    db = SessionLocal()
    try:
        transaction = (
            db.query(Transaction)
            .filter(Transaction.user_id == user_id)
            .one()
        )
        event = (
            db.query(FinancialEvent)
            .filter(FinancialEvent.legacy_transaction_id == transaction.id)
            .one()
        )
        assert db.query(Transaction).filter(Transaction.user_id == user_id).count() == 1
        assert db.query(FinancialEvent).filter(FinancialEvent.user_id == user_id).count() == 1
        assert db.query(FinancialEventEntry).filter(
            FinancialEventEntry.financial_event_id == event.id
        ).count() == 1
        assert db.query(FinancialEventHistory).filter(
            FinancialEventHistory.financial_event_id == event.id
        ).count() == 1
        assert db.query(CommandReceipt).filter(
            CommandReceipt.user_id == user_id,
            CommandReceipt.command_type == "CREATE_TRANSACTION",
            CommandReceipt.idempotency_key == key,
        ).count() == 1
    finally:
        db.close()


def test_connection_pool_exhaustion_recovers_after_checkout_is_released(
    isolated_single_connection_engine,
):
    held_connection = isolated_single_connection_engine.connect()
    try:
        with pytest.raises(SQLAlchemyTimeoutError):
            isolated_single_connection_engine.connect()
    finally:
        held_connection.close()

    with isolated_single_connection_engine.connect() as recovered_connection:
        assert recovered_connection.execute(text("SELECT 1")).scalar_one() == 1


@pytest.mark.parametrize("raise_error", [False, True], ids=["normal", "exception"])
def test_transaction_db_dependency_releases_pool_connection(
    isolated_single_connection_engine,
    monkeypatch,
    raise_error,
):
    isolated_session_factory = sessionmaker(
        autocommit=False,
        autoflush=False,
        bind=isolated_single_connection_engine,
    )
    monkeypatch.setattr(transaction_router, "SessionLocal", isolated_session_factory)

    dependency = transaction_router.get_db()
    db = next(dependency)
    assert db.execute(text("SELECT 1")).scalar_one() == 1
    assert isolated_single_connection_engine.pool.checkedout() == 1

    if raise_error:
        with pytest.raises(RuntimeError, match="simulated request failure"):
            dependency.throw(RuntimeError("simulated request failure"))
    else:
        with pytest.raises(StopIteration):
            next(dependency)

    assert isolated_single_connection_engine.pool.checkedout() == 0
    with isolated_single_connection_engine.connect() as recovered_connection:
        assert recovered_connection.execute(text("SELECT 1")).scalar_one() == 1


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


def test_postgres_deadlock_rolls_back_and_same_command_recovers():
    if engine.dialect.name != "postgresql":
        pytest.skip("Deadlock recovery test is PostgreSQL-specific")

    user_id, headers = create_user()
    created = client.post(
        "/transactions",
        json=expense_payload("100000.00", "deadlock-base"),
        headers=command_headers(headers),
    )
    assert created.status_code == 200
    transaction_id = created.json()["id"]
    expected_version = created.json()["canonical_version"]
    event_id = created.json()["canonical_event_id"]
    idempotency_key = uuid4().hex
    update_headers = command_headers(
        headers,
        key=idempotency_key,
        expected_version=expected_version,
    )
    update_payload = expense_payload("250000.00", "after-deadlock")

    event_claimed = Event()
    api_backend_pid = {}
    blocker = SessionLocal()

    def set_transaction_timeout(connection):
        connection.exec_driver_sql("SET LOCAL statement_timeout = '12000ms'")

    def capture_canonical_event_claim(
        connection,
        cursor,
        statement,
        parameters,
        context,
        executemany,
    ):
        normalized = " ".join(statement.lower().split())
        if normalized.startswith(
            "update financial_events set version=financial_events.version"
        ) and "pid" not in api_backend_pid:
            api_backend_pid["pid"] = connection.exec_driver_sql(
                "SELECT pg_backend_pid()"
            ).scalar_one()
            event_claimed.set()

    sqlalchemy_event.listen(engine, "begin", set_transaction_timeout)
    sqlalchemy_event.listen(engine, "after_cursor_execute", capture_canonical_event_claim)
    executor = ThreadPoolExecutor(max_workers=1)
    request_future = None
    blocker_sqlstate = None
    request_sqlstate = None
    first_response = None

    try:
        blocker.query(Transaction).filter(
            Transaction.id == transaction_id
        ).with_for_update().one()
        blocker_backend_pid = blocker.execute(
            text("SELECT pg_backend_pid()")
        ).scalar_one()

        request_future = executor.submit(
            client.put,
            f"/transactions/{transaction_id}",
            json=update_payload,
            headers=update_headers,
        )
        assert event_claimed.wait(timeout=10), "request never claimed the canonical event row"
        assert api_backend_pid["pid"] != blocker_backend_pid
        _wait_for_postgres_lock_wait(api_backend_pid["pid"])

        try:
            blocker.query(FinancialEvent).filter(
                FinancialEvent.id == event_id
            ).with_for_update().one()
            blocker.commit()
        except DBAPIError as exc:
            blocker_sqlstate = _postgres_sqlstate(exc)
            blocker.rollback()

        try:
            first_response = request_future.result(timeout=15)
        except BaseException as exc:
            request_sqlstate = _postgres_sqlstate(exc)

        assert blocker_sqlstate in {None, "40P01"}
        assert request_sqlstate in {None, "40P01"}
        assert (blocker_sqlstate == "40P01") != (request_sqlstate == "40P01"), (
            blocker_sqlstate,
            request_sqlstate,
        )
        if first_response is not None:
            assert first_response.status_code == 200
        else:
            assert request_sqlstate == "40P01"
    finally:
        blocker.rollback()
        blocker.close()
        sqlalchemy_event.remove(engine, "begin", set_transaction_timeout)
        sqlalchemy_event.remove(engine, "after_cursor_execute", capture_canonical_event_claim)
        if request_future is not None and not request_future.done():
            try:
                request_future.result(timeout=15)
            except BaseException:
                pass
        executor.shutdown(wait=True, cancel_futures=True)

    lock_check = SessionLocal()
    try:
        lock_check.query(Transaction).filter(
            Transaction.id == transaction_id
        ).with_for_update(nowait=True).one()
        lock_check.query(FinancialEvent).filter(
            FinancialEvent.id == event_id
        ).with_for_update(nowait=True).one()
        lock_check.rollback()
    finally:
        lock_check.close()

    if request_sqlstate == "40P01":
        db = SessionLocal()
        try:
            transaction = db.query(Transaction).filter(
                Transaction.id == transaction_id
            ).one()
            event_row = db.query(FinancialEvent).filter(
                FinancialEvent.id == event_id
            ).one()
            entry = db.query(FinancialEventEntry).filter(
                FinancialEventEntry.financial_event_id == event_id
            ).one()
            assert transaction.amount == Decimal("100000.00")
            assert event_row.version == expected_version
            assert entry.amount == Decimal("-100000.00")
            assert db.query(FinancialEventHistory).filter(
                FinancialEventHistory.financial_event_id == event_id
            ).count() == 1
            assert db.query(CommandReceipt).filter(
                CommandReceipt.user_id == user_id,
                CommandReceipt.command_type == "UPDATE_TRANSACTION",
                CommandReceipt.idempotency_key == idempotency_key,
            ).count() == 0
        finally:
            db.close()

    retry = client.put(
        f"/transactions/{transaction_id}",
        json=update_payload,
        headers=update_headers,
    )
    assert retry.status_code == 200
    if first_response is not None:
        assert retry.json() == first_response.json()

    db = SessionLocal()
    try:
        transaction = db.query(Transaction).filter(
            Transaction.id == transaction_id,
            Transaction.user_id == user_id,
        ).one()
        event_row = db.query(FinancialEvent).filter(
            FinancialEvent.id == event_id,
            FinancialEvent.user_id == user_id,
        ).one()
        entries = db.query(FinancialEventEntry).filter(
            FinancialEventEntry.financial_event_id == event_id
        ).all()
        history = db.query(FinancialEventHistory).filter(
            FinancialEventHistory.financial_event_id == event_id
        ).order_by(FinancialEventHistory.event_version.asc()).all()
        receipt = db.query(CommandReceipt).filter(
            CommandReceipt.user_id == user_id,
            CommandReceipt.command_type == "UPDATE_TRANSACTION",
            CommandReceipt.idempotency_key == idempotency_key,
        ).one()

        assert db.query(Transaction).filter(Transaction.user_id == user_id).count() == 1
        assert db.query(FinancialEvent).filter(FinancialEvent.user_id == user_id).count() == 1
        assert transaction.amount == Decimal("250000.00")
        assert transaction.description == "after-deadlock"
        assert event_row.version == expected_version + 1
        assert len(entries) == 1
        assert entries[0].amount == Decimal("-250000.00")
        assert [row.event_version for row in history] == [1, 2]
        assert [row.transition_type for row in history] == ["CREATED", "CORRECTED"]
        assert db.query(CommandReceipt).filter(
            CommandReceipt.user_id == user_id,
            CommandReceipt.command_type == "UPDATE_TRANSACTION",
            CommandReceipt.idempotency_key == idempotency_key,
        ).count() == 1
        assert receipt.response_body == retry.json()
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
