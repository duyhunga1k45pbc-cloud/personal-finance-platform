from __future__ import annotations

import datetime
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
