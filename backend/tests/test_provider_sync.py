from __future__ import annotations

import datetime
import multiprocessing
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.canonical_audit import audit_user
from app.database import SessionLocal, engine
from app.main import app
from app.models import (
    ExternalTransaction,
    ExternalTransactionEvidence,
    ProviderSyncCheckpoint,
    ProviderSyncPage,
    ProviderSyncPageEvidence,
    User,
)
from app.provider_sync_service import (
    ProviderSyncCheckpointConflict,
    ProviderSyncError,
    ProviderSyncObservation,
    ProviderSyncPageData,
    commit_sync_page,
    sync_provider_connection,
)


client = TestClient(app)


def create_user(prefix="sync"):
    email = f"{prefix}-{uuid.uuid4().hex}@example.com"
    response = client.post("/auth/register", json={"email": email, "password": "secret123"})
    assert response.status_code == 200
    login = client.post("/auth/login", json={"email": email, "password": "secret123"})
    assert login.status_code == 200
    db = SessionLocal()
    try:
        user_id = db.query(User.id).filter(User.email == email).scalar()
    finally:
        db.close()
    return user_id, {"Authorization": f"Bearer {login.json()['access_token']}"}


def create_connection(headers, *, suffix=None):
    response = client.post(
        "/provider-connections",
        json={
            "provider_name": "MockBank",
            "external_account_id": f"sync-acct-{suffix or uuid.uuid4().hex}",
        },
        headers={**headers, "Idempotency-Key": f"conn-{uuid.uuid4().hex}"},
    )
    assert response.status_code == 200
    return response.json()


def observation(external_id, minute, *, status="posted", amount="50000"):
    base = datetime.datetime(2026, 9, 3, 0, 0, tzinfo=datetime.timezone.utc)
    return ProviderSyncObservation(
        external_transaction_id=external_id,
        observed_at=base + datetime.timedelta(minutes=minute),
        raw_payload={"status": status, "amount": amount, "external_id": external_id},
    )


class FakeAdapter:
    def __init__(self, pages):
        self.pages = dict(pages)
        self.calls = []

    def fetch_page(self, cursor):
        self.calls.append(cursor)
        value = self.pages[cursor]
        if isinstance(value, BaseException):
            raise value
        return value


class ProcessAdapter:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def fetch_page(self, cursor):
        self.calls.append(cursor)
        return self.pages[cursor]


def _wait_at_process_boundary(connection):
    if not connection.poll(30):
        raise TimeoutError("parent did not release child at its test boundary")
    connection.recv()


def _provider_sync_child_before_commit(connection, user_id, connection_id, page):
    from app.database import engine as child_engine
    import app.provider_sync_service as sync_service

    try:
        child_engine.dispose(close=False)
        original_ingest = sync_service._ingest_external_evidence_locked
        signaled = False

        def flush_then_wait(*args, **kwargs):
            nonlocal signaled
            result = original_ingest(*args, **kwargs)
            if not signaled:
                db = args[0]
                backend_pid = db.execute(text("SELECT pg_backend_pid()")).scalar_one()
                uncommitted_transactions = db.query(ExternalTransaction).filter(
                    ExternalTransaction.provider_connection_id == connection_id
                ).count()
                uncommitted_evidence = db.query(ExternalTransactionEvidence).filter(
                    ExternalTransactionEvidence.provider_connection_id == connection_id
                ).count()
                connection.send(
                    {
                        "phase": "uncommitted",
                        "backend_pid": backend_pid,
                        "transactions": uncommitted_transactions,
                        "evidence": uncommitted_evidence,
                    }
                )
                signaled = True
                _wait_at_process_boundary(connection)
            return result

        sync_service._ingest_external_evidence_locked = flush_then_wait
        sync_service.commit_sync_page(
            SessionLocal,
            user_id=user_id,
            connection_id=connection_id,
            request_cursor=None,
            page=page,
        )
    except BaseException as exc:
        try:
            connection.send({"phase": "error", "error": repr(exc)})
        except (BrokenPipeError, EOFError, OSError):
            pass
        raise
    finally:
        connection.close()


def _provider_sync_child_after_commit(
    connection,
    user_id,
    connection_id,
    first_page,
):
    from app.database import engine as child_engine
    import app.provider_sync_service as sync_service

    try:
        child_engine.dispose(close=False)
        original_commit = sync_service.commit_sync_page

        def commit_then_wait(*args, **kwargs):
            result = original_commit(*args, **kwargs)
            connection.send(
                {
                    "phase": "committed",
                    "page_id": result.page_id,
                    "next_cursor": result.next_cursor,
                }
            )
            _wait_at_process_boundary(connection)
            return result

        sync_service.commit_sync_page = commit_then_wait
        sync_provider_connection(
            SessionLocal,
            user_id=user_id,
            connection_id=connection_id,
            adapter=ProcessAdapter({None: first_page}),
        )
    except BaseException as exc:
        try:
            connection.send({"phase": "error", "error": repr(exc)})
        except (BrokenPipeError, EOFError, OSError):
            pass
        raise
    finally:
        connection.close()


def _receive_process_phase(parent_connection, process, expected_phase, timeout=15):
    if not parent_connection.poll(timeout):
        process.join(timeout=0.1)
        pytest.fail(
            f"child did not reach {expected_phase!r}; exit code={process.exitcode}"
        )
    message = parent_connection.recv()
    assert message["phase"] == expected_phase, message
    return message


def _terminate_process(process):
    if process.is_alive():
        process.terminate()
    process.join(timeout=5)
    if process.is_alive():
        process.kill()
        process.join(timeout=5)
    assert not process.is_alive(), "provider-sync child did not terminate"


def _wait_for_backend_disconnect(backend_pid, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with engine.connect() as connection:
            active = connection.execute(
                text("SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE pid = :pid)"),
                {"pid": backend_pid},
            ).scalar_one()
        if not active:
            return
        time.sleep(0.05)
    pytest.fail(f"PostgreSQL backend {backend_pid} remained after child termination")


def _assert_sync_page_counts(connection_id, *, pages, external_transactions, evidence):
    db = SessionLocal()
    try:
        assert db.query(ProviderSyncPage).filter(
            ProviderSyncPage.provider_connection_id == connection_id
        ).count() == pages
        assert db.query(ExternalTransaction).filter(
            ExternalTransaction.provider_connection_id == connection_id
        ).count() == external_transactions
        assert db.query(ExternalTransactionEvidence).filter(
            ExternalTransactionEvidence.provider_connection_id == connection_id
        ).count() == evidence
        assert db.query(ProviderSyncPageEvidence).join(
            ProviderSyncPage,
            ProviderSyncPageEvidence.sync_page_id == ProviderSyncPage.id,
        ).filter(
            ProviderSyncPage.provider_connection_id == connection_id
        ).count() == evidence
    finally:
        db.close()


def test_sync_commits_page_and_advances_checkpoint_atomically():
    user_id, headers = create_user()
    connection = create_connection(headers)
    adapter = FakeAdapter(
        {
            None: ProviderSyncPageData(
                observations=[observation("txn-a", 1)],
                next_cursor="cursor-1",
                has_more=False,
            )
        }
    )

    result = sync_provider_connection(
        SessionLocal,
        user_id=user_id,
        connection_id=connection["id"],
        adapter=adapter,
    )
    assert result.start_cursor is None
    assert result.end_cursor == "cursor-1"
    assert result.pages_committed == 1
    assert result.evidence_created == 1

    db = SessionLocal()
    try:
        checkpoint = db.query(ProviderSyncCheckpoint).filter(
            ProviderSyncCheckpoint.provider_connection_id == connection["id"]
        ).one()
        page = db.query(ProviderSyncPage).filter(
            ProviderSyncPage.provider_connection_id == connection["id"]
        ).one()
        link = db.query(ProviderSyncPageEvidence).filter(
            ProviderSyncPageEvidence.sync_page_id == page.id
        ).one()
        assert checkpoint.committed_cursor == "cursor-1"
        assert checkpoint.version == 2
        assert page.checkpoint_version_before == 1
        assert page.checkpoint_version_after == 2
        assert page.observations_count == 1
        assert link.ordinal == 0
    finally:
        db.close()


def test_sync_multiple_pages_keeps_cursor_chain_and_trace():
    user_id, headers = create_user()
    connection = create_connection(headers)
    adapter = FakeAdapter(
        {
            None: ProviderSyncPageData(
                observations=[observation("txn-1", 1)],
                next_cursor="c1",
                has_more=True,
            ),
            "c1": ProviderSyncPageData(
                observations=[observation("txn-2", 2)],
                next_cursor="c2",
                has_more=False,
            ),
        }
    )
    result = sync_provider_connection(
        SessionLocal,
        user_id=user_id,
        connection_id=connection["id"],
        adapter=adapter,
    )
    assert adapter.calls == [None, "c1"]
    assert result.pages_committed == 2
    assert result.end_cursor == "c2"

    db = SessionLocal()
    try:
        pages = db.query(ProviderSyncPage).filter(
            ProviderSyncPage.provider_connection_id == connection["id"]
        ).order_by(ProviderSyncPage.checkpoint_version_after.asc()).all()
        assert [(p.request_cursor, p.next_cursor) for p in pages] == [(None, "c1"), ("c1", "c2")]
        assert [(p.checkpoint_version_before, p.checkpoint_version_after) for p in pages] == [(1, 2), (2, 3)]
        checkpoint = db.query(ProviderSyncCheckpoint).filter(
            ProviderSyncCheckpoint.provider_connection_id == connection["id"]
        ).one()
        assert checkpoint.version == 3
        assert checkpoint.committed_cursor == "c2"
    finally:
        db.close()


def test_same_external_transaction_across_pages_keeps_one_identity_multiple_evidence():
    user_id, headers = create_user()
    connection = create_connection(headers)
    external_id = f"txn-{uuid.uuid4().hex}"
    adapter = FakeAdapter(
        {
            None: ProviderSyncPageData(
                observations=[observation(external_id, 1, status="pending")],
                next_cursor="c1",
                has_more=True,
            ),
            "c1": ProviderSyncPageData(
                observations=[observation(external_id, 2, status="posted")],
                next_cursor="c2",
                has_more=False,
            ),
        }
    )
    sync_provider_connection(
        SessionLocal,
        user_id=user_id,
        connection_id=connection["id"],
        adapter=adapter,
    )

    db = SessionLocal()
    try:
        transactions = db.query(ExternalTransaction).filter(
            ExternalTransaction.provider_connection_id == connection["id"],
            ExternalTransaction.external_transaction_id == external_id,
        ).all()
        assert len(transactions) == 1
        evidence_count = db.query(ExternalTransactionEvidence).filter(
            ExternalTransactionEvidence.external_transaction_record_id == transactions[0].id
        ).count()
        assert evidence_count == 2
    finally:
        db.close()


def test_exact_page_replay_is_idempotent_without_checkpoint_increment():
    user_id, headers = create_user()
    connection = create_connection(headers)
    page = ProviderSyncPageData(
        observations=[observation("txn-replay", 1)],
        next_cursor="c1",
        has_more=False,
    )
    first = commit_sync_page(
        SessionLocal,
        user_id=user_id,
        connection_id=connection["id"],
        request_cursor=None,
        page=page,
    )
    replay = commit_sync_page(
        SessionLocal,
        user_id=user_id,
        connection_id=connection["id"],
        request_cursor=None,
        page=page,
    )
    assert first.replayed is False
    assert replay.replayed is True
    assert replay.page_id == first.page_id

    db = SessionLocal()
    try:
        checkpoint = db.query(ProviderSyncCheckpoint).filter(
            ProviderSyncCheckpoint.provider_connection_id == connection["id"]
        ).one()
        assert checkpoint.version == 2
        assert db.query(ProviderSyncPage).filter(
            ProviderSyncPage.provider_connection_id == connection["id"]
        ).count() == 1
        assert db.query(ExternalTransactionEvidence).filter(
            ExternalTransactionEvidence.provider_connection_id == connection["id"]
        ).count() == 1
    finally:
        db.close()


def test_failure_before_page_commit_rolls_back_evidence_and_checkpoint(monkeypatch):
    user_id, headers = create_user()
    connection = create_connection(headers)
    page = ProviderSyncPageData(
        observations=[observation("txn-ok-before-crash", 1), observation("txn-crash", 2)],
        next_cursor="c1",
        has_more=False,
    )

    import app.provider_sync_service as sync_service

    original = sync_service._ingest_external_evidence_locked
    calls = {"count": 0}

    def fail_on_second(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("simulated process failure before page commit")
        return original(*args, **kwargs)

    monkeypatch.setattr(sync_service, "_ingest_external_evidence_locked", fail_on_second)
    with pytest.raises(RuntimeError, match="simulated process failure"):
        commit_sync_page(
            SessionLocal,
            user_id=user_id,
            connection_id=connection["id"],
            request_cursor=None,
            page=page,
        )

    db = SessionLocal()
    try:
        checkpoint = db.query(ProviderSyncCheckpoint).filter(
            ProviderSyncCheckpoint.provider_connection_id == connection["id"]
        ).one()
        assert checkpoint.version == 1
        assert checkpoint.committed_cursor is None
        assert db.query(ProviderSyncPage).filter(
            ProviderSyncPage.provider_connection_id == connection["id"]
        ).count() == 0
        assert db.query(ExternalTransactionEvidence).filter(
            ExternalTransactionEvidence.provider_connection_id == connection["id"]
        ).count() == 0
    finally:
        db.close()


def test_process_termination_before_page_commit_rolls_back_all_page_effects():
    if engine.dialect.name != "postgresql":
        pytest.skip("Process termination recovery test requires PostgreSQL")
    if "fork" not in multiprocessing.get_all_start_methods():
        pytest.skip("Process termination recovery test requires fork support")

    user_id, headers = create_user()
    connection = create_connection(headers)
    external_id = f"terminated-before-commit-{uuid.uuid4().hex}"
    page = ProviderSyncPageData(
        observations=[observation(external_id, 40)],
        next_cursor="uncommitted-cursor",
        has_more=False,
    )
    context = multiprocessing.get_context("fork")
    parent_connection, child_connection = context.Pipe(duplex=True)
    process = context.Process(
        target=_provider_sync_child_before_commit,
        args=(child_connection, user_id, connection["id"], page),
    )
    process.start()
    child_connection.close()

    try:
        boundary = _receive_process_phase(
            parent_connection,
            process,
            "uncommitted",
        )
        assert boundary["transactions"] == 1
        assert boundary["evidence"] == 1

        db = SessionLocal()
        try:
            checkpoint = db.query(ProviderSyncCheckpoint).filter(
                ProviderSyncCheckpoint.provider_connection_id == connection["id"]
            ).one()
            assert checkpoint.committed_cursor is None
            assert checkpoint.version == 1
            assert db.query(ExternalTransaction).filter(
                ExternalTransaction.provider_connection_id == connection["id"],
                ExternalTransaction.external_transaction_id == external_id,
            ).count() == 0
            assert db.query(ExternalTransactionEvidence).filter(
                ExternalTransactionEvidence.provider_connection_id == connection["id"]
            ).count() == 0
        finally:
            db.close()

        _terminate_process(process)
        assert process.exitcode != 0
        _wait_for_backend_disconnect(boundary["backend_pid"])

        db = SessionLocal()
        try:
            checkpoint = db.query(ProviderSyncCheckpoint).filter(
                ProviderSyncCheckpoint.provider_connection_id == connection["id"]
            ).one()
            assert checkpoint.committed_cursor is None
            assert checkpoint.version == 1
            assert db.query(ExternalTransaction).filter(
                ExternalTransaction.provider_connection_id == connection["id"],
                ExternalTransaction.external_transaction_id == external_id,
            ).count() == 0
            assert db.query(ExternalTransactionEvidence).filter(
                ExternalTransactionEvidence.provider_connection_id == connection["id"]
            ).count() == 0
        finally:
            db.close()
        _assert_sync_page_counts(
            connection["id"],
            pages=0,
            external_transactions=0,
            evidence=0,
        )
    finally:
        _terminate_process(process)
        parent_connection.close()


def test_process_termination_after_page_commit_resumes_from_visible_checkpoint():
    if engine.dialect.name != "postgresql":
        pytest.skip("Process termination recovery test requires PostgreSQL")
    if "fork" not in multiprocessing.get_all_start_methods():
        pytest.skip("Process termination recovery test requires fork support")

    user_id, headers = create_user()
    connection = create_connection(headers)
    first_external_id = f"terminated-after-commit-first-{uuid.uuid4().hex}"
    second_external_id = f"terminated-after-commit-second-{uuid.uuid4().hex}"
    first_page = ProviderSyncPageData(
        observations=[observation(first_external_id, 41)],
        next_cursor="committed-cursor",
        has_more=True,
    )
    second_page = ProviderSyncPageData(
        observations=[observation(second_external_id, 42)],
        next_cursor="resumed-cursor",
        has_more=False,
    )
    context = multiprocessing.get_context("fork")
    parent_connection, child_connection = context.Pipe(duplex=True)
    process = context.Process(
        target=_provider_sync_child_after_commit,
        args=(child_connection, user_id, connection["id"], first_page),
    )
    process.start()
    child_connection.close()

    try:
        boundary = _receive_process_phase(
            parent_connection,
            process,
            "committed",
        )

        db = SessionLocal()
        try:
            checkpoint = db.query(ProviderSyncCheckpoint).filter(
                ProviderSyncCheckpoint.provider_connection_id == connection["id"]
            ).one()
            first_row = db.query(ProviderSyncPage).filter(
                ProviderSyncPage.id == boundary["page_id"],
                ProviderSyncPage.provider_connection_id == connection["id"],
            ).one()
            assert checkpoint.committed_cursor == "committed-cursor"
            assert checkpoint.version == 2
            assert first_row.request_cursor is None
            assert first_row.next_cursor == "committed-cursor"
            assert db.query(ExternalTransactionEvidence).filter(
                ExternalTransactionEvidence.provider_connection_id == connection["id"]
            ).count() == 1
        finally:
            db.close()
        _assert_sync_page_counts(
            connection["id"],
            pages=1,
            external_transactions=1,
            evidence=1,
        )

        _terminate_process(process)
        assert process.exitcode != 0

        adapter = ProcessAdapter({"committed-cursor": second_page})
        result = sync_provider_connection(
            SessionLocal,
            user_id=user_id,
            connection_id=connection["id"],
            adapter=adapter,
        )
        assert adapter.calls == ["committed-cursor"]
        assert result.start_cursor == "committed-cursor"
        assert result.end_cursor == "resumed-cursor"
        assert result.pages_committed == 1

        db = SessionLocal()
        try:
            checkpoint = db.query(ProviderSyncCheckpoint).filter(
                ProviderSyncCheckpoint.provider_connection_id == connection["id"]
            ).one()
            pages = db.query(ProviderSyncPage).filter(
                ProviderSyncPage.provider_connection_id == connection["id"]
            ).order_by(ProviderSyncPage.checkpoint_version_after.asc()).all()
            external_ids = {
                row.external_transaction_id
                for row in db.query(ExternalTransaction).filter(
                    ExternalTransaction.provider_connection_id == connection["id"]
                ).all()
            }
            assert checkpoint.committed_cursor == "resumed-cursor"
            assert checkpoint.version == 3
            assert [(row.request_cursor, row.next_cursor) for row in pages] == [
                (None, "committed-cursor"),
                ("committed-cursor", "resumed-cursor"),
            ]
            assert external_ids == {first_external_id, second_external_id}
        finally:
            db.close()
        _assert_sync_page_counts(
            connection["id"],
            pages=2,
            external_transactions=2,
            evidence=2,
        )
    finally:
        _terminate_process(process)
        parent_connection.close()


def test_worker_failure_after_first_committed_page_resumes_from_checkpoint():
    user_id, headers = create_user()
    connection = create_connection(headers)
    first_adapter = FakeAdapter(
        {
            None: ProviderSyncPageData(
                observations=[observation("txn-page-1", 1)],
                next_cursor="c1",
                has_more=True,
            ),
            "c1": RuntimeError("provider unavailable after first commit"),
        }
    )
    with pytest.raises(RuntimeError, match="provider unavailable"):
        sync_provider_connection(
            SessionLocal,
            user_id=user_id,
            connection_id=connection["id"],
            adapter=first_adapter,
        )

    retry_adapter = FakeAdapter(
        {
            "c1": ProviderSyncPageData(
                observations=[observation("txn-page-2", 2)],
                next_cursor="c2",
                has_more=False,
            )
        }
    )
    result = sync_provider_connection(
        SessionLocal,
        user_id=user_id,
        connection_id=connection["id"],
        adapter=retry_adapter,
    )
    assert retry_adapter.calls == ["c1"]
    assert result.start_cursor == "c1"
    assert result.end_cursor == "c2"


def test_stale_page_cannot_advance_checkpoint():
    user_id, headers = create_user()
    connection = create_connection(headers)
    commit_sync_page(
        SessionLocal,
        user_id=user_id,
        connection_id=connection["id"],
        request_cursor=None,
        page=ProviderSyncPageData(
            observations=[observation("winner", 1)],
            next_cursor="winner-cursor",
            has_more=False,
        ),
    )

    with pytest.raises(ProviderSyncCheckpointConflict) as exc_info:
        commit_sync_page(
            SessionLocal,
            user_id=user_id,
            connection_id=connection["id"],
            request_cursor=None,
            page=ProviderSyncPageData(
                observations=[observation("stale-loser", 2)],
                next_cursor="loser-cursor",
                has_more=False,
            ),
        )
    assert exc_info.value.actual_cursor == "winner-cursor"

    db = SessionLocal()
    try:
        checkpoint = db.query(ProviderSyncCheckpoint).filter(
            ProviderSyncCheckpoint.provider_connection_id == connection["id"]
        ).one()
        assert checkpoint.committed_cursor == "winner-cursor"
        assert db.query(ExternalTransaction).filter(
            ExternalTransaction.provider_connection_id == connection["id"],
            ExternalTransaction.external_transaction_id == "stale-loser",
        ).count() == 0
    finally:
        db.close()


def test_worker_refetches_after_concurrent_checkpoint_conflict():
    user_id, headers = create_user()
    connection = create_connection(headers)

    class RacingAdapter:
        def __init__(self):
            self.calls = []
            self.raced = False

        def fetch_page(self, cursor):
            self.calls.append(cursor)
            if cursor is None and not self.raced:
                self.raced = True
                commit_sync_page(
                    SessionLocal,
                    user_id=user_id,
                    connection_id=connection["id"],
                    request_cursor=None,
                    page=ProviderSyncPageData(
                        observations=[observation("rival-winner", 1)],
                        next_cursor="c1",
                        has_more=True,
                    ),
                )
                return ProviderSyncPageData(
                    observations=[observation("stale-fetch", 2)],
                    next_cursor="stale-cursor",
                    has_more=True,
                )
            assert cursor == "c1"
            return ProviderSyncPageData(
                observations=[observation("after-race", 3)],
                next_cursor="c2",
                has_more=False,
            )

    adapter = RacingAdapter()
    result = sync_provider_connection(
        SessionLocal,
        user_id=user_id,
        connection_id=connection["id"],
        adapter=adapter,
    )
    assert adapter.calls == [None, "c1"]
    assert result.checkpoint_conflicts == 1
    assert result.end_cursor == "c2"

    db = SessionLocal()
    try:
        assert db.query(ExternalTransaction).filter(
            ExternalTransaction.provider_connection_id == connection["id"],
            ExternalTransaction.external_transaction_id == "stale-fetch",
        ).count() == 0
    finally:
        db.close()


def test_has_more_requires_cursor_progress():
    user_id, headers = create_user()
    connection = create_connection(headers)
    with pytest.raises(ProviderSyncError, match="without advancing the cursor"):
        commit_sync_page(
            SessionLocal,
            user_id=user_id,
            connection_id=connection["id"],
            request_cursor="same",
            page=ProviderSyncPageData(
                observations=[],
                next_cursor="same",
                has_more=True,
            ),
        )


def test_sync_checkpoint_endpoints_are_owner_scoped():
    user_id, headers = create_user()
    _, other_headers = create_user("sync-other")
    connection = create_connection(headers)

    before = client.get(f"/provider-connections/{connection['id']}/sync-checkpoint", headers=headers)
    assert before.status_code == 200
    assert before.json()["initialized"] is True
    assert before.json()["version"] == 1
    assert before.json()["committed_cursor"] is None

    commit_sync_page(
        SessionLocal,
        user_id=user_id,
        connection_id=connection["id"],
        request_cursor=None,
        page=ProviderSyncPageData(
            observations=[observation("endpoint-txn", 1)],
            next_cursor="c1",
            has_more=False,
        ),
    )
    checkpoint = client.get(f"/provider-connections/{connection['id']}/sync-checkpoint", headers=headers)
    pages = client.get(f"/provider-connections/{connection['id']}/sync-pages", headers=headers)
    assert checkpoint.status_code == 200
    assert checkpoint.json()["committed_cursor"] == "c1"
    assert checkpoint.json()["pages_committed"] == 1
    assert pages.status_code == 200
    assert len(pages.json()) == 1
    assert len(pages.json()[0]["evidence_ids"]) == 1

    assert client.get(f"/provider-connections/{connection['id']}/sync-checkpoint", headers=other_headers).status_code == 404
    assert client.get(f"/provider-connections/{connection['id']}/sync-pages", headers=other_headers).status_code == 404


def test_provider_sync_audit_is_green():
    user_id, headers = create_user()
    connection = create_connection(headers)
    adapter = FakeAdapter(
        {
            None: ProviderSyncPageData(
                observations=[observation("audit-1", 1)],
                next_cursor="c1",
                has_more=True,
            ),
            "c1": ProviderSyncPageData(
                observations=[observation("audit-2", 2)],
                next_cursor="c2",
                has_more=False,
            ),
        }
    )
    sync_provider_connection(
        SessionLocal,
        user_id=user_id,
        connection_id=connection["id"],
        adapter=adapter,
    )
    db = SessionLocal()
    try:
        result = audit_user(db, user_id)
        assert result["ok"] is True
        assert result["provider_sync_divergence"] is None
    finally:
        db.close()


def test_provider_sync_audit_detects_checkpoint_cursor_divergence_without_committing_corruption():
    user_id, headers = create_user()
    connection = create_connection(headers)
    commit_sync_page(
        SessionLocal,
        user_id=user_id,
        connection_id=connection["id"],
        request_cursor=None,
        page=ProviderSyncPageData(
            observations=[observation("audit-corrupt", 1)],
            next_cursor="correct-cursor",
            has_more=False,
        ),
    )

    db = SessionLocal()
    try:
        checkpoint = db.query(ProviderSyncCheckpoint).filter(
            ProviderSyncCheckpoint.provider_connection_id == connection["id"]
        ).one()
        checkpoint.committed_cursor = "wrong-cursor"
        db.flush()
        result = audit_user(db, user_id)
        assert result["ok"] is False
        assert result["provider_sync_divergence"]["reason"] == "provider_sync_checkpoint_cursor_not_latest_page"
        db.rollback()
    finally:
        db.close()


@pytest.mark.skipif(engine.dialect.name != "postgresql", reason="PostgreSQL trigger invariant")
def test_postgresql_provider_sync_trace_is_append_only():
    user_id, headers = create_user()
    connection = create_connection(headers)
    commit_sync_page(
        SessionLocal,
        user_id=user_id,
        connection_id=connection["id"],
        request_cursor=None,
        page=ProviderSyncPageData(
            observations=[observation("immutable-sync", 1)],
            next_cursor="c1",
            has_more=False,
        ),
    )

    db = SessionLocal()
    try:
        page = db.query(ProviderSyncPage).filter(
            ProviderSyncPage.provider_connection_id == connection["id"]
        ).one()
        with pytest.raises(Exception):
            db.execute(
                text("UPDATE provider_sync_pages SET next_cursor = :cursor WHERE id = :id"),
                {"cursor": "tampered", "id": page.id},
            )
            db.flush()
        db.rollback()

        link = db.query(ProviderSyncPageEvidence).filter(
            ProviderSyncPageEvidence.sync_page_id == page.id
        ).one()
        with pytest.raises(Exception):
            db.execute(
                text("DELETE FROM provider_sync_page_evidence WHERE id = :id"),
                {"id": link.id},
            )
            db.flush()
        db.rollback()
    finally:
        db.close()
