from __future__ import annotations

import datetime
import uuid
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.database import SessionLocal, engine
from app.main import app
from app.models import (
    FinancialEvent,
    FinancialEventEntry,
    ProviderInterpretationHistory,
    ProviderNormalizedCandidate,
    ProviderTransactionInterpretation,
)
from app.canonical_audit import audit_user

client = TestClient(app)


def create_user(prefix="interpret"):
    email = f"{prefix}-{uuid.uuid4().hex}@example.com"
    response = client.post("/auth/register", json={"email": email, "password": "secret123"})
    assert response.status_code == 200
    login = client.post("/auth/login", json={"email": email, "password": "secret123"})
    assert login.status_code == 200
    token = login.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def create_account(headers, account_type="BANK"):
    response = client.post(
        "/accounts",
        json={"name": f"acct-{uuid.uuid4().hex[:8]}", "account_type": account_type, "currency": "VND"},
        headers=headers,
    )
    assert response.status_code == 200
    return response.json()


def provider_transaction(headers, payload=None):
    connection = client.post(
        "/provider-connections",
        json={"provider_name": "MockBank", "external_account_id": f"acct-{uuid.uuid4().hex}"},
        headers={**headers, "Idempotency-Key": f"conn-{uuid.uuid4().hex}"},
    )
    assert connection.status_code == 200
    observed = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    ingestion = client.post(
        f"/provider-connections/{connection.json()['id']}/external-transactions",
        json={
            "external_transaction_id": f"txn-{uuid.uuid4().hex}",
            "observed_at": observed.isoformat(),
            "raw_payload": payload or {"amount": "50000", "description": "provider raw"},
        },
        headers={**headers, "Idempotency-Key": f"ingest-{uuid.uuid4().hex}"},
    )
    assert ingestion.status_code == 200
    return connection.json(), ingestion.json(), observed


def normalize(headers, connection_id, transaction_id, evidence_id, *, status="POSTED", currency="VND", direction="OUTFLOW", amount="50000.00", version="mock-v1", key=None):
    response = client.post(
        f"/provider-connections/{connection_id}/external-transactions/{transaction_id}/normalizations",
        json={
            "evidence_id": evidence_id,
            "normalizer_version": version,
            "amount": amount,
            "currency": currency,
            "direction": direction,
            "normalized_status": status,
            "occurred_at": datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat(),
            "description": "normalized provider transaction",
            "provider_status": status.lower(),
        },
        headers={**headers, "Idempotency-Key": key or f"norm-{uuid.uuid4().hex}"},
    )
    return response


def test_normalization_creates_immutable_candidate_and_unclassified_interpretation():
    headers = create_user()
    connection, ingested, _ = provider_transaction(headers)
    response = normalize(headers, connection["id"], ingested["external_transaction_record_id"], ingested["evidence_id"])
    assert response.status_code == 200
    body = response.json()
    assert body["interpretation"]["state"] == "UNCLASSIFIED"
    assert body["interpretation"]["event_type"] is None
    assert body["candidate"]["currency"] == "VND"
    assert body["candidate"]["direction"] == "OUTFLOW"


def test_same_evidence_and_normalizer_version_cannot_change_normalized_meaning():
    headers = create_user()
    connection, ingested, _ = provider_transaction(headers)
    first = normalize(headers, connection["id"], ingested["external_transaction_record_id"], ingested["evidence_id"], version="stable-v1")
    assert first.status_code == 200
    second = normalize(headers, connection["id"], ingested["external_transaction_record_id"], ingested["evidence_id"], version="stable-v1", amount="60000.00")
    assert second.status_code == 409


def test_system_classification_moves_unclassified_to_classified():
    headers = create_user()
    connection, ingested, _ = provider_transaction(headers)
    normalized = normalize(headers, connection["id"], ingested["external_transaction_record_id"], ingested["evidence_id"]).json()
    interpretation = normalized["interpretation"]
    response = client.post(
        f"/provider-connections/{connection['id']}/external-transactions/{ingested['external_transaction_record_id']}/classification",
        json={"event_type": "EXPENSE", "reason": "classifier guess"},
        headers={**headers, "Idempotency-Key": f"classify-{uuid.uuid4().hex}", "X-Expected-Version": str(interpretation["version"])},
    )
    assert response.status_code == 200
    assert response.json()["state"] == "CLASSIFIED"
    assert response.json()["confidence"] == "INFERRED"


def test_user_confirmation_can_override_system_classification_and_materializes_posted_vnd():
    headers = create_user()
    account = create_account(headers)
    connection, ingested, _ = provider_transaction(headers)
    normalized = normalize(headers, connection["id"], ingested["external_transaction_record_id"], ingested["evidence_id"], direction="INFLOW").json()
    classified = client.post(
        f"/provider-connections/{connection['id']}/external-transactions/{ingested['external_transaction_record_id']}/classification",
        json={"event_type": "TRANSFER"},
        headers={**headers, "Idempotency-Key": f"classify-{uuid.uuid4().hex}", "X-Expected-Version": str(normalized["interpretation"]["version"])},
    ).json()
    confirmed = client.post(
        f"/provider-connections/{connection['id']}/external-transactions/{ingested['external_transaction_record_id']}/confirm",
        json={"event_type": "INCOME", "account_id": account["id"], "reason": "salary confirmed"},
        headers={**headers, "Idempotency-Key": f"confirm-{uuid.uuid4().hex}", "X-Expected-Version": str(classified["version"])},
    )
    assert confirmed.status_code == 200
    body = confirmed.json()
    assert body["state"] == "USER_CONFIRMED"
    assert body["event_type"] == "INCOME"
    assert body["canonical_event_id"] is not None
    assert body["materialization_blocker"] is None

    db = SessionLocal()
    try:
        event = db.query(FinancialEvent).filter(FinancialEvent.id == body["canonical_event_id"]).one()
        entry = db.query(FinancialEventEntry).filter(FinancialEventEntry.financial_event_id == event.id).one()
        assert event.provenance == "PROVIDER"
        assert event.interpretation_state == "USER_CONFIRMED"
        assert Decimal(entry.amount) == Decimal("50000.00")
    finally:
        db.close()


def test_posted_expense_materializes_negative_entry():
    headers = create_user()
    account = create_account(headers)
    connection, ingested, _ = provider_transaction(headers)
    normalized = normalize(headers, connection["id"], ingested["external_transaction_record_id"], ingested["evidence_id"], direction="OUTFLOW").json()
    confirmed = client.post(
        f"/provider-connections/{connection['id']}/external-transactions/{ingested['external_transaction_record_id']}/confirm",
        json={"event_type": "EXPENSE", "account_id": account["id"]},
        headers={**headers, "Idempotency-Key": f"confirm-{uuid.uuid4().hex}", "X-Expected-Version": str(normalized["interpretation"]["version"])},
    )
    assert confirmed.status_code == 200
    db = SessionLocal()
    try:
        event_id = confirmed.json()["canonical_event_id"]
        entry = db.query(FinancialEventEntry).filter(FinancialEventEntry.financial_event_id == event_id).one()
        assert Decimal(entry.amount) == Decimal("-50000.00")
    finally:
        db.close()


def test_pending_user_confirmation_preserves_semantics_without_touching_canonical_state():
    headers = create_user()
    account = create_account(headers)
    connection, ingested, _ = provider_transaction(headers)
    normalized = normalize(headers, connection["id"], ingested["external_transaction_record_id"], ingested["evidence_id"], status="PENDING").json()
    confirmed = client.post(
        f"/provider-connections/{connection['id']}/external-transactions/{ingested['external_transaction_record_id']}/confirm",
        json={"event_type": "EXPENSE", "account_id": account["id"]},
        headers={**headers, "Idempotency-Key": f"confirm-{uuid.uuid4().hex}", "X-Expected-Version": str(normalized["interpretation"]["version"])},
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["state"] == "USER_CONFIRMED"
    assert confirmed.json()["canonical_event_id"] is None
    assert confirmed.json()["materialization_blocker"] == "NOT_POSTED"


def test_non_vnd_can_be_semantically_confirmed_but_not_silently_forced_into_vnd_canonical_state():
    headers = create_user()
    account = create_account(headers)
    connection, ingested, _ = provider_transaction(headers, {"amount": "12.34", "currency": "USD"})
    normalized = normalize(headers, connection["id"], ingested["external_transaction_record_id"], ingested["evidence_id"], currency="USD", amount="12.34").json()
    confirmed = client.post(
        f"/provider-connections/{connection['id']}/external-transactions/{ingested['external_transaction_record_id']}/confirm",
        json={"event_type": "EXPENSE", "account_id": account["id"]},
        headers={**headers, "Idempotency-Key": f"confirm-{uuid.uuid4().hex}", "X-Expected-Version": str(normalized["interpretation"]["version"])},
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["canonical_event_id"] is None
    assert confirmed.json()["materialization_blocker"] == "UNSUPPORTED_CURRENCY"


def test_transfer_can_be_user_confirmed_but_requires_specialized_multi_account_materialization():
    headers = create_user()
    account = create_account(headers)
    connection, ingested, _ = provider_transaction(headers)
    normalized = normalize(headers, connection["id"], ingested["external_transaction_record_id"], ingested["evidence_id"]).json()
    confirmed = client.post(
        f"/provider-connections/{connection['id']}/external-transactions/{ingested['external_transaction_record_id']}/confirm",
        json={"event_type": "TRANSFER", "account_id": account["id"]},
        headers={**headers, "Idempotency-Key": f"confirm-{uuid.uuid4().hex}", "X-Expected-Version": str(normalized["interpretation"]["version"])},
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["state"] == "USER_CONFIRMED"
    assert confirmed.json()["canonical_event_id"] is None
    assert confirmed.json()["materialization_blocker"] == "SPECIALIZED_SEMANTICS_REQUIRED"


def test_system_cannot_overwrite_user_confirmed_interpretation():
    headers = create_user()
    account = create_account(headers)
    connection, ingested, _ = provider_transaction(headers)
    normalized = normalize(headers, connection["id"], ingested["external_transaction_record_id"], ingested["evidence_id"]).json()
    confirmed = client.post(
        f"/provider-connections/{connection['id']}/external-transactions/{ingested['external_transaction_record_id']}/confirm",
        json={"event_type": "EXPENSE", "account_id": account["id"]},
        headers={**headers, "Idempotency-Key": f"confirm-{uuid.uuid4().hex}", "X-Expected-Version": str(normalized["interpretation"]["version"])},
    ).json()
    response = client.post(
        f"/provider-connections/{connection['id']}/external-transactions/{ingested['external_transaction_record_id']}/classification",
        json={"event_type": "TRANSFER"},
        headers={**headers, "Idempotency-Key": f"classify-{uuid.uuid4().hex}", "X-Expected-Version": str(confirmed["version"])},
    )
    assert response.status_code == 409


def test_stale_expected_version_is_rejected():
    headers = create_user()
    connection, ingested, _ = provider_transaction(headers)
    normalized = normalize(headers, connection["id"], ingested["external_transaction_record_id"], ingested["evidence_id"]).json()
    version = normalized["interpretation"]["version"]
    first = client.post(
        f"/provider-connections/{connection['id']}/external-transactions/{ingested['external_transaction_record_id']}/classification",
        json={"event_type": "EXPENSE"},
        headers={**headers, "Idempotency-Key": f"classify-{uuid.uuid4().hex}", "X-Expected-Version": str(version)},
    )
    assert first.status_code == 200
    stale = client.post(
        f"/provider-connections/{connection['id']}/external-transactions/{ingested['external_transaction_record_id']}/classification",
        json={"event_type": "TRANSFER"},
        headers={**headers, "Idempotency-Key": f"classify-{uuid.uuid4().hex}", "X-Expected-Version": str(version)},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "stale_version"


def test_new_normalization_resets_unconfirmed_system_classification():
    headers = create_user()
    connection, ingested, observed = provider_transaction(headers)
    normalized = normalize(headers, connection["id"], ingested["external_transaction_record_id"], ingested["evidence_id"], version="v1").json()
    classified = client.post(
        f"/provider-connections/{connection['id']}/external-transactions/{ingested['external_transaction_record_id']}/classification",
        json={"event_type": "EXPENSE"},
        headers={**headers, "Idempotency-Key": f"classify-{uuid.uuid4().hex}", "X-Expected-Version": str(normalized["interpretation"]["version"])},
    )
    assert classified.status_code == 200

    second_evidence = client.post(
        f"/provider-connections/{connection['id']}/external-transactions",
        json={
            "external_transaction_id": ingested["external_transaction_id"],
            "observed_at": (observed + datetime.timedelta(minutes=1)).isoformat(),
            "raw_payload": {"amount": "50000", "status": "posted", "new": True},
        },
        headers={**headers, "Idempotency-Key": f"ingest-{uuid.uuid4().hex}"},
    )
    assert second_evidence.status_code == 200
    refreshed = normalize(headers, connection["id"], ingested["external_transaction_record_id"], second_evidence.json()["evidence_id"], version="v2")
    assert refreshed.status_code == 200
    assert refreshed.json()["interpretation"]["state"] == "UNCLASSIFIED"
    assert refreshed.json()["interpretation"]["event_type"] is None


def test_materialized_user_confirmation_is_not_silently_rebased_by_new_provider_evidence():
    headers = create_user()
    account = create_account(headers)
    connection, ingested, observed = provider_transaction(headers)
    normalized = normalize(headers, connection["id"], ingested["external_transaction_record_id"], ingested["evidence_id"], version="v1").json()
    confirmed = client.post(
        f"/provider-connections/{connection['id']}/external-transactions/{ingested['external_transaction_record_id']}/confirm",
        json={"event_type": "EXPENSE", "account_id": account["id"]},
        headers={**headers, "Idempotency-Key": f"confirm-{uuid.uuid4().hex}", "X-Expected-Version": str(normalized["interpretation"]["version"])},
    ).json()
    original_candidate_id = confirmed["normalized_candidate_id"]

    second_evidence = client.post(
        f"/provider-connections/{connection['id']}/external-transactions",
        json={
            "external_transaction_id": ingested["external_transaction_id"],
            "observed_at": (observed + datetime.timedelta(minutes=2)).isoformat(),
            "raw_payload": {"amount": "60000", "status": "corrected-provider-observation"},
        },
        headers={**headers, "Idempotency-Key": f"ingest-{uuid.uuid4().hex}"},
    )
    refreshed = normalize(headers, connection["id"], ingested["external_transaction_record_id"], second_evidence.json()["evidence_id"], version="v2", amount="60000.00")
    assert refreshed.status_code == 200
    assert refreshed.json()["interpretation_locked"] is True
    assert refreshed.json()["interpretation"]["normalized_candidate_id"] == original_candidate_id


def test_provider_interpretation_audit_is_green_for_valid_state():
    headers = create_user()
    account = create_account(headers)
    connection, ingested, _ = provider_transaction(headers)
    normalized = normalize(headers, connection["id"], ingested["external_transaction_record_id"], ingested["evidence_id"]).json()
    confirmed = client.post(
        f"/provider-connections/{connection['id']}/external-transactions/{ingested['external_transaction_record_id']}/confirm",
        json={"event_type": "EXPENSE", "account_id": account["id"]},
        headers={**headers, "Idempotency-Key": f"confirm-{uuid.uuid4().hex}", "X-Expected-Version": str(normalized["interpretation"]["version"])},
    )
    assert confirmed.status_code == 200
    db = SessionLocal()
    try:
        interp = db.query(ProviderTransactionInterpretation).filter(ProviderTransactionInterpretation.id == confirmed.json()["id"]).one()
        result = audit_user(db, interp.user_id)
        assert result["provider_interpretation_divergence"] is None
        assert result["ok"] is True
    finally:
        db.close()


def test_normalized_candidate_and_interpretation_history_are_append_only_in_postgresql():
    if engine.dialect.name != "postgresql":
        pytest.skip("Database-level append-only trigger is PostgreSQL-specific")
    headers = create_user()
    connection, ingested, _ = provider_transaction(headers)
    body = normalize(headers, connection["id"], ingested["external_transaction_record_id"], ingested["evidence_id"]).json()
    candidate_id = body["candidate"]["id"]
    interpretation_id = body["interpretation"]["id"]
    db = SessionLocal()
    try:
        with pytest.raises(Exception):
            db.execute(text("UPDATE provider_normalized_candidates SET description='tamper' WHERE id=:id"), {"id": candidate_id})
            db.commit()
        db.rollback()
        history = db.query(ProviderInterpretationHistory).filter(ProviderInterpretationHistory.interpretation_id == interpretation_id).first()
        with pytest.raises(Exception):
            db.execute(text("DELETE FROM provider_interpretation_history WHERE id=:id"), {"id": history.id})
            db.commit()
        db.rollback()
    finally:
        db.close()
