from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from fastapi.encoders import jsonable_encoder
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import CommandReceipt


class IdempotencyConflictError(ValueError):
    pass


@dataclass(frozen=True)
class StoredCommandResponse:
    status_code: int
    body: dict[str, Any]


def normalize_idempotency_key(value: str) -> str:
    key = value.strip()
    if not key:
        raise ValueError("Idempotency-Key must not be blank")
    if len(key) > 128:
        raise ValueError("Idempotency-Key must be at most 128 characters")
    return key


def hash_command(payload: dict[str, Any]) -> str:
    encoded = jsonable_encoder(payload)
    serialized = json.dumps(
        encoded,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def get_stored_command_response(
    db: Session,
    *,
    user_id: int,
    command_type: str,
    idempotency_key: str,
    request_hash: str,
) -> StoredCommandResponse | None:
    receipt = (
        db.query(CommandReceipt)
        .filter(
            CommandReceipt.user_id == user_id,
            CommandReceipt.command_type == command_type,
            CommandReceipt.idempotency_key == idempotency_key,
        )
        .one_or_none()
    )
    if receipt is None:
        return None
    if receipt.request_hash != request_hash:
        raise IdempotencyConflictError(
            "Idempotency key was already used for a different command payload"
        )
    return StoredCommandResponse(
        status_code=receipt.response_status,
        body=receipt.response_body,
    )


def add_command_receipt(
    db: Session,
    *,
    user_id: int,
    command_type: str,
    idempotency_key: str,
    request_hash: str,
    transaction_id: int | None = None,
    financial_event_id: int | None = None,
    reconciliation_id: int | None = None,
    provider_connection_id: int | None = None,
    external_evidence_id: int | None = None,
    response_status: int,
    response_body: dict[str, Any],
) -> CommandReceipt:
    targets = [transaction_id, financial_event_id, reconciliation_id, provider_connection_id, external_evidence_id]
    if sum(value is not None for value in targets) != 1:
        raise ValueError(
            "Command receipt must reference exactly one transaction, financial event, reconciliation, provider connection, or external evidence"
        )

    receipt = CommandReceipt(
        user_id=user_id,
        command_type=command_type,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        transaction_id=transaction_id,
        financial_event_id=financial_event_id,
        reconciliation_id=reconciliation_id,
        provider_connection_id=provider_connection_id,
        external_evidence_id=external_evidence_id,
        response_status=response_status,
        response_body=jsonable_encoder(response_body),
    )
    db.add(receipt)
    return receipt


def commit_with_idempotency_race_recovery(
    db: Session,
    *,
    user_id: int,
    command_type: str,
    idempotency_key: str,
    request_hash: str,
) -> StoredCommandResponse | None:
    """Commit the command transaction.

    Returns None when this transaction won the unique idempotency-key race.
    If another transaction committed the same key first, rolls back all local
    side effects and returns that already-committed response instead.
    """

    try:
        db.commit()
        return None
    except IntegrityError:
        db.rollback()
        existing = get_stored_command_response(
            db,
            user_id=user_id,
            command_type=command_type,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
        if existing is None:
            raise
        return existing
