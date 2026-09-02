from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import DBAPIError

from app.canonical_audit import audit_user, find_first_provider_evidence_divergence
from app.database import Base, SessionLocal, engine
from app.main import app
from app.models import (
    CommandReceipt,
    ExternalTransaction,
    ExternalTransactionEvidence,
    FinancialEvent,
    ProviderConnection,
)

client = TestClient(app)
Base.metadata.create_all(bind=engine)


def create_user(prefix="provider"):
    email = f"{prefix}_{uuid4().hex}@example.com"
    password = "testpassword123"
    register = client.post("/auth/register", json={"email": email, "password": password})
    assert register.status_code == 200
    login = client.post("/auth/login", json={"email": email, "password": password})
    assert login.status_code == 200
    return register.json()["id"], {"Authorization": f"Bearer {login.json()['access_token']}"}


def with_key(headers, key=None):
    result = dict(headers)
    result["Idempotency-Key"] = key or uuid4().hex
    return result


def create_connection(headers, *, key=None, provider="mockbank", external_account_id="acct-001"):
    return client.post(
        "/provider-connections",
        json={
            "provider_name": provider,
            "external_account_id": external_account_id,
            "display_name": "Primary provider account",
        },
        headers=with_key(headers, key),
    )


def ingest(headers, connection_id, *, key=None, external_id="txn-001", observed_at="2026-09-03T00:00:00Z", raw_payload=None):
    return client.post(
        f"/provider-connections/{connection_id}/external-transactions",
        json={
            "external_transaction_id": external_id,
            "observed_at": observed_at,
            "raw_payload": raw_payload or {
                "id": external_id,
                "amount": "100000.00",
                "currency": "VND",
                "status": "pending",
            },
        },
        headers=with_key(headers, key),
    )


def test_create_provider_connection_is_user_owned_and_idempotent():
    user_id, headers = create_user()
    key = uuid4().hex
    first = create_connection(headers, key=key)
    second = create_connection(headers, key=key)
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == second.json()
    assert first.json()["provider_name"] == "MOCKBANK"

    db = SessionLocal()
    try:
        assert db.query(ProviderConnection).filter(ProviderConnection.user_id == user_id).count() == 1
        assert db.query(CommandReceipt).filter(
            CommandReceipt.user_id == user_id,
            CommandReceipt.command_type == "CREATE_PROVIDER_CONNECTION",
        ).count() == 1
    finally:
        db.close()


def test_same_connection_identity_with_different_command_key_deduplicates_connection():
    _, headers = create_user()
    first = create_connection(headers)
    second = create_connection(headers)
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert second.json()["deduplicated"] is True


def test_ingest_creates_stable_external_identity_and_immutable_raw_evidence_only():
    user_id, headers = create_user()
    connection = create_connection(headers).json()
    raw = {
        "id": "provider-42",
        "amount": "12.34",
        "currency": "USD",
        "status": "provider-specific-value",
        "nested": {"untouched": True},
    }
    response = ingest(
        headers,
        connection["id"],
        external_id="provider-42",
        raw_payload=raw,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["canonicalized"] is False
    assert body["deduplicated"] is False

    evidence = client.get(
        f"/provider-connections/{connection['id']}/external-transactions/{body['external_transaction_record_id']}/evidence",
        headers=headers,
    )
    assert evidence.status_code == 200
    assert evidence.json()[0]["raw_payload"] == raw

    db = SessionLocal()
    try:
        assert db.query(ExternalTransaction).filter(ExternalTransaction.user_id == user_id).count() == 1
        assert db.query(ExternalTransactionEvidence).filter(ExternalTransactionEvidence.user_id == user_id).count() == 1
        assert db.query(FinancialEvent).filter(
            FinancialEvent.user_id == user_id,
            FinancialEvent.provenance == "PROVIDER",
        ).count() == 0
    finally:
        db.close()


def test_same_external_transaction_can_receive_new_immutable_observation():
    user_id, headers = create_user()
    connection = create_connection(headers).json()
    first = ingest(
        headers,
        connection["id"],
        external_id="txn-stateful",
        observed_at="2026-09-03T00:00:00Z",
        raw_payload={"id": "txn-stateful", "status": "pending", "amount": "100.00"},
    )
    second = ingest(
        headers,
        connection["id"],
        external_id="txn-stateful",
        observed_at="2026-09-03T01:00:00Z",
        raw_payload={"id": "txn-stateful", "status": "posted", "amount": "100.00"},
    )
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["external_transaction_record_id"] == second.json()["external_transaction_record_id"]
    assert first.json()["evidence_id"] != second.json()["evidence_id"]

    db = SessionLocal()
    try:
        assert db.query(ExternalTransaction).filter(ExternalTransaction.user_id == user_id).count() == 1
        assert db.query(ExternalTransactionEvidence).filter(ExternalTransactionEvidence.user_id == user_id).count() == 2
    finally:
        db.close()


def test_ingest_retry_same_key_replays_without_duplicate_evidence():
    user_id, headers = create_user()
    connection = create_connection(headers).json()
    key = uuid4().hex
    first = ingest(headers, connection["id"], key=key, external_id="retry-me")
    second = ingest(headers, connection["id"], key=key, external_id="retry-me")
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == second.json()

    db = SessionLocal()
    try:
        assert db.query(ExternalTransactionEvidence).filter(ExternalTransactionEvidence.user_id == user_id).count() == 1
        assert db.query(CommandReceipt).filter(
            CommandReceipt.user_id == user_id,
            CommandReceipt.command_type == "INGEST_EXTERNAL_EVIDENCE",
        ).count() == 1
    finally:
        db.close()


def test_same_exact_observation_with_different_key_is_evidence_deduplicated():
    _, headers = create_user()
    connection = create_connection(headers).json()
    first = ingest(headers, connection["id"], external_id="same-observation")
    second = ingest(headers, connection["id"], external_id="same-observation")
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["evidence_id"] == second.json()["evidence_id"]
    assert second.json()["deduplicated"] is True


def test_reusing_idempotency_key_with_different_provider_payload_is_conflict():
    _, headers = create_user()
    connection = create_connection(headers).json()
    key = uuid4().hex
    first = ingest(
        headers,
        connection["id"],
        key=key,
        external_id="conflict",
        raw_payload={"id": "conflict", "status": "pending"},
    )
    second = ingest(
        headers,
        connection["id"],
        key=key,
        external_id="conflict",
        raw_payload={"id": "conflict", "status": "posted"},
    )
    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["detail"]["code"] == "idempotency_conflict"


def test_provider_connection_and_evidence_are_user_scoped():
    _, owner_headers = create_user("provider-owner")
    _, other_headers = create_user("provider-other")
    connection = create_connection(owner_headers).json()

    hidden = client.get(f"/provider-connections/{connection['id']}", headers=other_headers)
    assert hidden.status_code == 404

    blocked_ingest = ingest(other_headers, connection["id"], external_id="attack")
    assert blocked_ingest.status_code == 404


def test_provider_audit_detects_owner_divergence_without_committing_corruption():
    user_id, headers = create_user()
    other_user_id, other_headers = create_user("other-provider-owner")
    connection = create_connection(headers).json()
    ingest(headers, connection["id"], external_id="audit-me")

    db = SessionLocal()
    try:
        row = db.query(ProviderConnection).filter(ProviderConnection.id == connection["id"]).one()

        # Deliberately create only the ownership inconsistency that the audit
        # is supposed to detect. Using a random existing ProviderConnection
        # owner can collide with the DB uniqueness invariant
        # (user_id, provider_name, external_account_id) before the audit runs.
        row.user_id = other_user_id
        db.flush()
        divergence = find_first_provider_evidence_divergence(db, user_id)
        assert divergence is not None
        assert divergence["reason"] == "provider_connection_owner_mismatch"
        db.rollback()
        assert audit_user(db, user_id)["provider_evidence_divergence"] is None
    finally:
        db.rollback()
        db.close()


def test_external_transaction_identity_is_immutable_in_postgresql():
    if engine.dialect.name != "postgresql":
        pytest.skip("Database-level external transaction identity trigger is PostgreSQL-specific")

    _, headers = create_user()
    connection = create_connection(headers).json()
    body = ingest(headers, connection["id"], external_id="immutable-id").json()

    db = SessionLocal()
    try:
        row = db.query(ExternalTransaction).filter(ExternalTransaction.id == body["external_transaction_record_id"]).one()
        row.external_transaction_id = "tampered"
        with pytest.raises(DBAPIError):
            db.commit()
    finally:
        db.rollback()
        db.close()


def test_raw_external_evidence_is_immutable_in_postgresql():
    if engine.dialect.name != "postgresql":
        pytest.skip("Database-level raw evidence trigger is PostgreSQL-specific")

    _, headers = create_user()
    connection = create_connection(headers).json()
    body = ingest(headers, connection["id"], external_id="immutable-evidence").json()

    db = SessionLocal()
    try:
        row = db.query(ExternalTransactionEvidence).filter(ExternalTransactionEvidence.id == body["evidence_id"]).one()
        row.raw_payload = {"tampered": True}
        with pytest.raises(DBAPIError):
            db.commit()
    finally:
        db.rollback()
        db.close()
