from __future__ import annotations

import datetime
import uuid
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.canonical_audit import audit_user
from app.database import SessionLocal, engine
from app.main import app
from app.models import (
    FinancialEvent,
    FinancialEventEntry,
    FinancialEventLink,
    ProviderTransactionLifecycle,
    ProviderTransactionLifecycleHistory,
)


client = TestClient(app)


def create_user(prefix="lifecycle"):
    email = f"{prefix}-{uuid.uuid4().hex}@example.com"
    response = client.post("/auth/register", json={"email": email, "password": "secret123"})
    assert response.status_code == 200
    login = client.post("/auth/login", json={"email": email, "password": "secret123"})
    assert login.status_code == 200
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def create_account(headers):
    response = client.post(
        "/accounts",
        json={"name": f"bank-{uuid.uuid4().hex[:8]}", "account_type": "BANK", "currency": "VND"},
        headers=headers,
    )
    assert response.status_code == 200
    return response.json()


def create_connection(headers):
    response = client.post(
        "/provider-connections",
        json={"provider_name": "MockBank", "external_account_id": f"acct-{uuid.uuid4().hex}"},
        headers={**headers, "Idempotency-Key": f"conn-{uuid.uuid4().hex}"},
    )
    assert response.status_code == 200
    return response.json()


def ingest(headers, connection_id, external_id, observed_at, *, raw_status):
    response = client.post(
        f"/provider-connections/{connection_id}/external-transactions",
        json={
            "external_transaction_id": external_id,
            "observed_at": observed_at.isoformat(),
            "raw_payload": {"amount": "50000", "status": raw_status},
        },
        headers={**headers, "Idempotency-Key": f"ingest-{uuid.uuid4().hex}"},
    )
    assert response.status_code == 200
    return response.json()


def normalize(headers, connection_id, transaction_id, evidence_id, *, status, direction="OUTFLOW"):
    return client.post(
        f"/provider-connections/{connection_id}/external-transactions/{transaction_id}/normalizations",
        json={
            "evidence_id": evidence_id,
            "normalizer_version": f"normalizer-{uuid.uuid4().hex}",
            "amount": "50000.00",
            "currency": "VND",
            "direction": direction,
            "normalized_status": status,
            "occurred_at": datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat(),
            "description": f"provider {status.lower()}",
            "provider_status": status.lower(),
        },
        headers={**headers, "Idempotency-Key": f"norm-{uuid.uuid4().hex}"},
    )


def confirm_expense(headers, connection_id, transaction_id, account_id, version):
    return client.post(
        f"/provider-connections/{connection_id}/external-transactions/{transaction_id}/confirm",
        json={"event_type": "EXPENSE", "account_id": account_id, "reason": "user confirmed purchase"},
        headers={
            **headers,
            "Idempotency-Key": f"confirm-{uuid.uuid4().hex}",
            "X-Expected-Version": str(version),
        },
    )


def get_lifecycle(headers, connection_id, transaction_id):
    return client.get(
        f"/provider-connections/{connection_id}/external-transactions/{transaction_id}/lifecycle",
        headers=headers,
    )


def setup_first_observation(status="PENDING"):
    headers = create_user()
    connection = create_connection(headers)
    external_id = f"txn-{uuid.uuid4().hex}"
    observed = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    evidence = ingest(headers, connection["id"], external_id, observed, raw_status=status.lower())
    normalized = normalize(
        headers,
        connection["id"],
        evidence["external_transaction_record_id"],
        evidence["evidence_id"],
        status=status,
    )
    assert normalized.status_code == 200
    return headers, connection, external_id, observed, evidence, normalized.json()


def test_pending_observation_creates_source_lifecycle_without_canonical_state():
    headers, connection, _, observed, evidence, _ = setup_first_observation("PENDING")
    response = get_lifecycle(headers, connection["id"], evidence["external_transaction_record_id"])
    assert response.status_code == 200
    body = response.json()
    assert body["current_status"] == "PENDING"
    assert body["posted_candidate_id"] is None
    assert body["reversed_candidate_id"] is None
    assert body["canonical_event_id"] is None
    assert body["reversal_event_id"] is None
    assert body["version"] == 1
    assert body["current_observed_at"].startswith(observed.isoformat()[:19])


def test_first_observation_may_already_be_posted():
    headers, connection, _, _, evidence, _ = setup_first_observation("POSTED")
    body = get_lifecycle(headers, connection["id"], evidence["external_transaction_record_id"]).json()
    assert body["current_status"] == "POSTED"
    assert body["posted_candidate_id"] == body["current_candidate_id"]
    assert body["canonical_event_id"] is None


def test_pending_confirmation_then_posted_materializes_exactly_one_canonical_event():
    headers, connection, external_id, observed, evidence, first = setup_first_observation("PENDING")
    account = create_account(headers)
    confirmed = confirm_expense(
        headers,
        connection["id"],
        evidence["external_transaction_record_id"],
        account["id"],
        first["interpretation"]["version"],
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["canonical_event_id"] is None

    posted_evidence = ingest(
        headers,
        connection["id"],
        external_id,
        observed + datetime.timedelta(minutes=1),
        raw_status="posted",
    )
    posted = normalize(
        headers,
        connection["id"],
        evidence["external_transaction_record_id"],
        posted_evidence["evidence_id"],
        status="POSTED",
    )
    assert posted.status_code == 200
    canonical_event_id = posted.json()["interpretation"]["canonical_event_id"]
    assert canonical_event_id is not None

    lifecycle = get_lifecycle(headers, connection["id"], evidence["external_transaction_record_id"]).json()
    assert lifecycle["current_status"] == "POSTED"
    assert lifecycle["canonical_event_id"] == canonical_event_id

    db = SessionLocal()
    try:
        count = db.query(FinancialEvent).filter(FinancialEvent.id == canonical_event_id).count()
        assert count == 1
    finally:
        db.close()


def test_repeated_posted_observation_does_not_duplicate_canonical_event():
    headers, connection, external_id, observed, evidence, first = setup_first_observation("POSTED")
    account = create_account(headers)
    confirmed = confirm_expense(
        headers,
        connection["id"],
        evidence["external_transaction_record_id"],
        account["id"],
        first["interpretation"]["version"],
    )
    assert confirmed.status_code == 200
    original_event_id = confirmed.json()["canonical_event_id"]

    repeated = ingest(
        headers,
        connection["id"],
        external_id,
        observed + datetime.timedelta(minutes=1),
        raw_status="posted-again",
    )
    normalized = normalize(
        headers,
        connection["id"],
        evidence["external_transaction_record_id"],
        repeated["evidence_id"],
        status="POSTED",
    )
    assert normalized.status_code == 200
    assert normalized.json()["interpretation_locked"] is True

    lifecycle = get_lifecycle(headers, connection["id"], evidence["external_transaction_record_id"]).json()
    assert lifecycle["canonical_event_id"] == original_event_id
    assert lifecycle["current_status"] == "POSTED"

    db = SessionLocal()
    try:
        provider_events = db.query(FinancialEvent).filter(
            FinancialEvent.user_id == db.query(FinancialEvent.user_id).filter(FinancialEvent.id == original_event_id).scalar(),
            FinancialEvent.provenance == "PROVIDER",
            FinancialEvent.event_type == "EXPENSE",
        ).all()
        assert [event.id for event in provider_events].count(original_event_id) == 1
    finally:
        db.close()


def test_posted_to_reversed_creates_exact_provider_causal_reversal():
    headers, connection, external_id, observed, evidence, first = setup_first_observation("POSTED")
    account = create_account(headers)
    confirmed = confirm_expense(
        headers,
        connection["id"],
        evidence["external_transaction_record_id"],
        account["id"],
        first["interpretation"]["version"],
    )
    assert confirmed.status_code == 200
    original_event_id = confirmed.json()["canonical_event_id"]

    reversed_evidence = ingest(
        headers,
        connection["id"],
        external_id,
        observed + datetime.timedelta(minutes=2),
        raw_status="reversed",
    )
    reversed_normalized = normalize(
        headers,
        connection["id"],
        evidence["external_transaction_record_id"],
        reversed_evidence["evidence_id"],
        status="REVERSED",
    )
    assert reversed_normalized.status_code == 200
    assert reversed_normalized.json()["interpretation_locked"] is True

    lifecycle = get_lifecycle(headers, connection["id"], evidence["external_transaction_record_id"]).json()
    assert lifecycle["current_status"] == "REVERSED"
    assert lifecycle["canonical_event_id"] == original_event_id
    assert lifecycle["reversal_event_id"] is not None

    db = SessionLocal()
    try:
        original_entry = db.query(FinancialEventEntry).filter(FinancialEventEntry.financial_event_id == original_event_id).one()
        reversal = db.query(FinancialEvent).filter(FinancialEvent.id == lifecycle["reversal_event_id"]).one()
        reversal_entry = db.query(FinancialEventEntry).filter(FinancialEventEntry.financial_event_id == reversal.id).one()
        link = db.query(FinancialEventLink).filter(FinancialEventLink.from_event_id == reversal.id).one()
        assert reversal.event_type == "REVERSAL"
        assert reversal.provenance == "PROVIDER"
        assert reversal.confidence == "OBSERVED"
        assert Decimal(reversal_entry.amount) == -Decimal(original_entry.amount)
        assert link.relation_type == "REVERSAL_OF"
        assert link.to_event_id == original_event_id
    finally:
        db.close()


def test_repeated_reversed_observation_does_not_create_second_reversal():
    headers, connection, external_id, observed, evidence, first = setup_first_observation("POSTED")
    account = create_account(headers)
    confirmed = confirm_expense(headers, connection["id"], evidence["external_transaction_record_id"], account["id"], first["interpretation"]["version"])
    original_event_id = confirmed.json()["canonical_event_id"]

    for minute in (1, 2):
        reversal_evidence = ingest(headers, connection["id"], external_id, observed + datetime.timedelta(minutes=minute), raw_status=f"reversed-{minute}")
        response = normalize(headers, connection["id"], evidence["external_transaction_record_id"], reversal_evidence["evidence_id"], status="REVERSED")
        assert response.status_code == 200

    db = SessionLocal()
    try:
        links = db.query(FinancialEventLink).filter(
            FinancialEventLink.to_event_id == original_event_id,
            FinancialEventLink.relation_type == "REVERSAL_OF",
        ).all()
        assert len(links) == 1
    finally:
        db.close()


def test_newer_posted_to_pending_regression_is_rejected():
    headers, connection, external_id, observed, evidence, _ = setup_first_observation("POSTED")
    pending_evidence = ingest(headers, connection["id"], external_id, observed + datetime.timedelta(minutes=1), raw_status="pending-again")
    response = normalize(headers, connection["id"], evidence["external_transaction_record_id"], pending_evidence["evidence_id"], status="PENDING")
    assert response.status_code == 409
    assert "POSTED -> PENDING" in str(response.json()["detail"])


def test_newer_reversed_to_posted_regression_is_rejected():
    headers, connection, external_id, observed, evidence, _ = setup_first_observation("REVERSED")
    posted_evidence = ingest(headers, connection["id"], external_id, observed + datetime.timedelta(minutes=1), raw_status="posted-after-reversed")
    response = normalize(headers, connection["id"], evidence["external_transaction_record_id"], posted_evidence["evidence_id"], status="POSTED")
    assert response.status_code == 409
    assert "REVERSED -> POSTED" in str(response.json()["detail"])


def test_older_pending_observation_normalized_late_does_not_regress_posted_state():
    headers = create_user()
    connection = create_connection(headers)
    external_id = f"txn-{uuid.uuid4().hex}"
    base = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    older = ingest(headers, connection["id"], external_id, base, raw_status="pending")
    newer = ingest(headers, connection["id"], external_id, base + datetime.timedelta(minutes=1), raw_status="posted")

    posted = normalize(headers, connection["id"], newer["external_transaction_record_id"], newer["evidence_id"], status="POSTED")
    assert posted.status_code == 200
    late_pending = normalize(headers, connection["id"], older["external_transaction_record_id"], older["evidence_id"], status="PENDING")
    assert late_pending.status_code == 200
    assert late_pending.json()["lifecycle_ignored_stale"] is True
    assert late_pending.json()["interpretation"]["normalized_candidate_id"] == posted.json()["candidate"]["id"]

    lifecycle = get_lifecycle(headers, connection["id"], newer["external_transaction_record_id"]).json()
    assert lifecycle["current_status"] == "POSTED"
    assert lifecycle["current_candidate_id"] == posted.json()["candidate"]["id"]


def test_pending_to_reversed_without_materialized_event_keeps_financial_state_untouched():
    headers, connection, external_id, observed, evidence, _ = setup_first_observation("PENDING")
    reversed_evidence = ingest(headers, connection["id"], external_id, observed + datetime.timedelta(minutes=1), raw_status="reversed")
    response = normalize(headers, connection["id"], evidence["external_transaction_record_id"], reversed_evidence["evidence_id"], status="REVERSED")
    assert response.status_code == 200
    lifecycle = get_lifecycle(headers, connection["id"], evidence["external_transaction_record_id"]).json()
    assert lifecycle["current_status"] == "REVERSED"
    assert lifecycle["canonical_event_id"] is None
    assert lifecycle["reversal_event_id"] is None


def test_lifecycle_endpoint_is_user_scoped():
    headers, connection, _, _, evidence, _ = setup_first_observation("POSTED")
    other = create_user("other-lifecycle")
    response = get_lifecycle(other, connection["id"], evidence["external_transaction_record_id"])
    assert response.status_code == 404


def test_provider_lifecycle_audit_is_green_for_valid_reversal_chain():
    headers, connection, external_id, observed, evidence, first = setup_first_observation("POSTED")
    account = create_account(headers)
    confirmed = confirm_expense(headers, connection["id"], evidence["external_transaction_record_id"], account["id"], first["interpretation"]["version"])
    assert confirmed.status_code == 200
    reversed_evidence = ingest(headers, connection["id"], external_id, observed + datetime.timedelta(minutes=1), raw_status="reversed")
    response = normalize(headers, connection["id"], evidence["external_transaction_record_id"], reversed_evidence["evidence_id"], status="REVERSED")
    assert response.status_code == 200

    db = SessionLocal()
    try:
        lifecycle = db.query(ProviderTransactionLifecycle).filter(
            ProviderTransactionLifecycle.external_transaction_record_id == evidence["external_transaction_record_id"]
        ).one()
        result = audit_user(db, lifecycle.user_id)
        assert result["provider_lifecycle_divergence"] is None
        assert result["ok"] is True
    finally:
        db.close()


def test_provider_lifecycle_history_is_append_only_in_postgresql():
    if engine.dialect.name != "postgresql":
        pytest.skip("Database-level append-only trigger is PostgreSQL-specific")
    _, _, _, _, evidence, _ = setup_first_observation("POSTED")
    db = SessionLocal()
    try:
        lifecycle = db.query(ProviderTransactionLifecycle).filter(
            ProviderTransactionLifecycle.external_transaction_record_id == evidence["external_transaction_record_id"]
        ).one()
        history = db.query(ProviderTransactionLifecycleHistory).filter(
            ProviderTransactionLifecycleHistory.lifecycle_id == lifecycle.id
        ).first()
        with pytest.raises(Exception):
            db.execute(text("DELETE FROM provider_transaction_lifecycle_history WHERE id=:id"), {"id": history.id})
            db.commit()
        db.rollback()
    finally:
        db.close()
