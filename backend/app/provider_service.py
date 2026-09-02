from __future__ import annotations

import datetime
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import ExternalTransaction, ExternalTransactionEvidence, ProviderConnection


@dataclass(frozen=True)
class ConnectionResult:
    connection: ProviderConnection
    created: bool


@dataclass(frozen=True)
class EvidenceResult:
    transaction: ExternalTransaction
    evidence: ExternalTransactionEvidence
    created_evidence: bool


def normalize_provider_name(value: str) -> str:
    normalized = value.strip().upper()
    if not normalized:
        raise ValueError("provider_name must not be blank")
    return normalized


def normalize_external_identifier(value: str, *, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be blank")
    return normalized


def normalize_observed_at(value: datetime.datetime | None) -> datetime.datetime:
    if value is None:
        return datetime.datetime.now(datetime.timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.timezone.utc)
    return value.astimezone(datetime.timezone.utc)


def hash_raw_payload(payload: dict[str, Any]) -> str:
    serialized = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def get_owned_provider_connection(
    db: Session,
    *,
    user_id: int,
    connection_id: int,
) -> ProviderConnection | None:
    return (
        db.query(ProviderConnection)
        .filter(
            ProviderConnection.id == connection_id,
            ProviderConnection.user_id == user_id,
        )
        .one_or_none()
    )


def get_or_create_provider_connection(
    db: Session,
    *,
    user_id: int,
    provider_name: str,
    external_account_id: str,
    display_name: str | None,
) -> ConnectionResult:
    provider = normalize_provider_name(provider_name)
    account_id = normalize_external_identifier(
        external_account_id,
        field_name="external_account_id",
    )

    existing = (
        db.query(ProviderConnection)
        .filter(
            ProviderConnection.user_id == user_id,
            ProviderConnection.provider_name == provider,
            ProviderConnection.external_account_id == account_id,
        )
        .one_or_none()
    )
    if existing is not None:
        return ConnectionResult(existing, False)

    connection = ProviderConnection(
        user_id=user_id,
        provider_name=provider,
        external_account_id=account_id,
        display_name=display_name,
    )

    try:
        with db.begin_nested():
            db.add(connection)
            db.flush()
        return ConnectionResult(connection, True)
    except IntegrityError:
        existing = (
            db.query(ProviderConnection)
            .filter(
                ProviderConnection.user_id == user_id,
                ProviderConnection.provider_name == provider,
                ProviderConnection.external_account_id == account_id,
            )
            .one()
        )
        return ConnectionResult(existing, False)


def _get_or_create_external_transaction(
    db: Session,
    *,
    user_id: int,
    connection: ProviderConnection,
    external_transaction_id: str,
    observed_at: datetime.datetime,
) -> tuple[ExternalTransaction, bool]:
    provider_transaction_id = normalize_external_identifier(
        external_transaction_id,
        field_name="external_transaction_id",
    )
    existing = (
        db.query(ExternalTransaction)
        .filter(
            ExternalTransaction.provider_connection_id == connection.id,
            ExternalTransaction.external_transaction_id == provider_transaction_id,
        )
        .one_or_none()
    )
    if existing is not None:
        return existing, False

    transaction = ExternalTransaction(
        user_id=user_id,
        provider_connection_id=connection.id,
        external_transaction_id=provider_transaction_id,
        first_observed_at=observed_at,
    )
    try:
        with db.begin_nested():
            db.add(transaction)
            db.flush()
        return transaction, True
    except IntegrityError:
        existing = (
            db.query(ExternalTransaction)
            .filter(
                ExternalTransaction.provider_connection_id == connection.id,
                ExternalTransaction.external_transaction_id == provider_transaction_id,
            )
            .one()
        )
        return existing, False


def ingest_external_evidence(
    db: Session,
    *,
    user_id: int,
    connection: ProviderConnection,
    external_transaction_id: str,
    raw_payload: dict[str, Any],
    observed_at: datetime.datetime | None,
) -> EvidenceResult:
    if connection.user_id != user_id:
        raise ValueError("Provider connection ownership mismatch")

    observed = normalize_observed_at(observed_at)
    payload_hash = hash_raw_payload(raw_payload)
    transaction, _ = _get_or_create_external_transaction(
        db,
        user_id=user_id,
        connection=connection,
        external_transaction_id=external_transaction_id,
        observed_at=observed,
    )

    existing = (
        db.query(ExternalTransactionEvidence)
        .filter(
            ExternalTransactionEvidence.external_transaction_record_id == transaction.id,
            ExternalTransactionEvidence.payload_sha256 == payload_hash,
            ExternalTransactionEvidence.observed_at == observed,
        )
        .one_or_none()
    )
    if existing is not None:
        return EvidenceResult(transaction, existing, False)

    evidence = ExternalTransactionEvidence(
        user_id=user_id,
        provider_connection_id=connection.id,
        external_transaction_record_id=transaction.id,
        observed_at=observed,
        raw_payload=raw_payload,
        payload_sha256=payload_hash,
    )
    try:
        with db.begin_nested():
            db.add(evidence)
            db.flush()
        return EvidenceResult(transaction, evidence, True)
    except IntegrityError:
        existing = (
            db.query(ExternalTransactionEvidence)
            .filter(
                ExternalTransactionEvidence.external_transaction_record_id == transaction.id,
                ExternalTransactionEvidence.payload_sha256 == payload_hash,
                ExternalTransactionEvidence.observed_at == observed,
            )
            .one()
        )
        return EvidenceResult(transaction, existing, False)
