from __future__ import annotations

import datetime
import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Protocol, Sequence

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    ExternalTransaction,
    ExternalTransactionEvidence,
    ProviderConnection,
    ProviderSyncCheckpoint,
    ProviderSyncPage,
    ProviderSyncPageEvidence,
)
from app.provider_service import (
    EvidenceResult,
    hash_raw_payload,
    normalize_external_identifier,
    normalize_observed_at,
)


class ProviderSyncError(RuntimeError):
    pass


class ProviderSyncCheckpointConflict(ProviderSyncError):
    def __init__(self, *, expected_cursor: str | None, actual_cursor: str | None):
        self.expected_cursor = expected_cursor
        self.actual_cursor = actual_cursor
        super().__init__(
            f"Provider sync checkpoint changed concurrently: expected {expected_cursor!r}, "
            f"actual {actual_cursor!r}"
        )


@dataclass(frozen=True)
class ProviderSyncObservation:
    external_transaction_id: str
    observed_at: datetime.datetime
    raw_payload: dict


@dataclass(frozen=True)
class ProviderSyncPageData:
    observations: tuple[ProviderSyncObservation, ...]
    next_cursor: str | None
    has_more: bool

    def __init__(
        self,
        *,
        observations: Sequence[ProviderSyncObservation],
        next_cursor: str | None,
        has_more: bool,
    ):
        object.__setattr__(self, "observations", tuple(observations))
        object.__setattr__(self, "next_cursor", next_cursor)
        object.__setattr__(self, "has_more", bool(has_more))


class ProviderSyncAdapter(Protocol):
    def fetch_page(self, cursor: str | None) -> ProviderSyncPageData:
        """Fetch one provider page after the supplied durable cursor.

        The adapter owns provider-specific API semantics. It must map them to an
        opaque durable cursor suitable for retry/resume. Network I/O happens before
        any database transaction is opened by the worker.
        """


@dataclass(frozen=True)
class ProviderSyncPageCommit:
    page_id: int
    request_cursor: str | None
    next_cursor: str | None
    has_more: bool
    observations_count: int
    evidence_created: int
    evidence_deduplicated: int
    checkpoint_version_after: int
    replayed: bool


@dataclass(frozen=True)
class ProviderSyncResult:
    connection_id: int
    start_cursor: str | None
    end_cursor: str | None
    pages_committed: int
    pages_replayed: int
    checkpoint_conflicts: int
    evidence_created: int
    evidence_deduplicated: int


def _cursor_key(value: str | None) -> str:
    return "<NULL>" if value is None else value


def _utc_iso(value: datetime.datetime) -> str:
    normalized = normalize_observed_at(value)
    return normalized.isoformat()


def sync_page_hash(
    *,
    request_cursor: str | None,
    next_cursor: str | None,
    has_more: bool,
    observations: Sequence[ProviderSyncObservation],
) -> str:
    payload = {
        "request_cursor": request_cursor,
        "next_cursor": next_cursor,
        "has_more": bool(has_more),
        "observations": [
            {
                "external_transaction_id": observation.external_transaction_id.strip(),
                "observed_at": _utc_iso(observation.observed_at),
                "raw_payload": observation.raw_payload,
            }
            for observation in observations
        ],
    }
    serialized = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=lambda value: str(value) if isinstance(value, Decimal) else value,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def get_sync_checkpoint(
    db: Session,
    *,
    user_id: int,
    connection_id: int,
) -> ProviderSyncCheckpoint | None:
    return (
        db.query(ProviderSyncCheckpoint)
        .filter(
            ProviderSyncCheckpoint.user_id == user_id,
            ProviderSyncCheckpoint.provider_connection_id == connection_id,
        )
        .one_or_none()
    )


def _get_checkpoint_locked(
    db: Session,
    *,
    user_id: int,
    connection_id: int,
) -> ProviderSyncCheckpoint:
    connection = (
        db.query(ProviderConnection)
        .filter(
            ProviderConnection.id == connection_id,
            ProviderConnection.user_id == user_id,
        )
        .one_or_none()
    )
    if connection is None:
        raise ProviderSyncError("Provider sync connection not found or ownership mismatch")

    checkpoint = (
        db.query(ProviderSyncCheckpoint)
        .filter(ProviderSyncCheckpoint.provider_connection_id == connection_id)
        .with_for_update()
        .one_or_none()
    )
    if checkpoint is None:
        raise ProviderSyncError(
            "Provider sync checkpoint missing; migrate Task 12 and create connections through the provider service"
        )
    if checkpoint.user_id != user_id:
        raise ProviderSyncError("Provider sync checkpoint owner mismatch")
    return checkpoint


def _ingest_external_evidence_locked(
    db: Session,
    *,
    user_id: int,
    connection: ProviderConnection,
    observation: ProviderSyncObservation,
) -> EvidenceResult:
    """Ingest one observation inside the already-locked page transaction.

    Unlike the public single-evidence ingestion helper, this path intentionally uses
    no nested savepoints. The checkpoint row serializes page commits for one provider
    connection, so the whole page can roll back as one atomic unit.
    """

    external_transaction_id = normalize_external_identifier(
        observation.external_transaction_id,
        field_name="external_transaction_id",
    )
    observed_at = normalize_observed_at(observation.observed_at)
    payload_hash = hash_raw_payload(observation.raw_payload)

    transaction = (
        db.query(ExternalTransaction)
        .filter(
            ExternalTransaction.provider_connection_id == connection.id,
            ExternalTransaction.external_transaction_id == external_transaction_id,
        )
        .one_or_none()
    )
    if transaction is None:
        transaction = ExternalTransaction(
            user_id=user_id,
            provider_connection_id=connection.id,
            external_transaction_id=external_transaction_id,
            first_observed_at=observed_at,
        )
        db.add(transaction)
        db.flush()
    elif transaction.user_id != user_id:
        raise ProviderSyncError("External transaction ownership mismatch during provider sync")

    evidence = (
        db.query(ExternalTransactionEvidence)
        .filter(
            ExternalTransactionEvidence.external_transaction_record_id == transaction.id,
            ExternalTransactionEvidence.payload_sha256 == payload_hash,
            ExternalTransactionEvidence.observed_at == observed_at,
        )
        .one_or_none()
    )
    if evidence is not None:
        return EvidenceResult(transaction, evidence, False)

    evidence = ExternalTransactionEvidence(
        user_id=user_id,
        provider_connection_id=connection.id,
        external_transaction_record_id=transaction.id,
        observed_at=observed_at,
        raw_payload=observation.raw_payload,
        payload_sha256=payload_hash,
    )
    db.add(evidence)
    db.flush()
    return EvidenceResult(transaction, evidence, True)

def _stored_page_commit(page: ProviderSyncPage) -> ProviderSyncPageCommit:
    return ProviderSyncPageCommit(
        page_id=page.id,
        request_cursor=page.request_cursor,
        next_cursor=page.next_cursor,
        has_more=page.has_more,
        observations_count=page.observations_count,
        evidence_created=page.evidence_created,
        evidence_deduplicated=page.evidence_deduplicated,
        checkpoint_version_after=page.checkpoint_version_after,
        replayed=True,
    )


def commit_sync_page(
    session_factory: Callable[[], Session],
    *,
    user_id: int,
    connection_id: int,
    request_cursor: str | None,
    page: ProviderSyncPageData,
) -> ProviderSyncPageCommit:
    """Atomically persist one fetched provider page and advance its checkpoint.

    The provider fetch must already have completed. Evidence rows, page trace rows,
    and the durable cursor advance are committed in the same database transaction.
    If anything fails before commit, the cursor cannot move ahead of evidence.
    """

    if page.has_more and page.next_cursor == request_cursor:
        raise ProviderSyncError(
            "Provider sync adapter returned has_more=true without advancing the cursor"
        )

    digest = sync_page_hash(
        request_cursor=request_cursor,
        next_cursor=page.next_cursor,
        has_more=page.has_more,
        observations=page.observations,
    )

    db = session_factory()
    try:
        with db.begin():
            checkpoint = _get_checkpoint_locked(
                db,
                user_id=user_id,
                connection_id=connection_id,
            )

            existing_page = (
                db.query(ProviderSyncPage)
                .filter(
                    ProviderSyncPage.provider_connection_id == connection_id,
                    ProviderSyncPage.page_hash == digest,
                )
                .one_or_none()
            )
            if existing_page is not None:
                return _stored_page_commit(existing_page)

            if checkpoint.committed_cursor != request_cursor:
                raise ProviderSyncCheckpointConflict(
                    expected_cursor=request_cursor,
                    actual_cursor=checkpoint.committed_cursor,
                )

            connection = (
                db.query(ProviderConnection)
                .filter(
                    ProviderConnection.id == connection_id,
                    ProviderConnection.user_id == user_id,
                )
                .one()
            )

            evidence_results: list[EvidenceResult] = []
            for observation in page.observations:
                evidence_results.append(
                    _ingest_external_evidence_locked(
                        db,
                        user_id=user_id,
                        connection=connection,
                        observation=observation,
                    )
                )

            created_count = sum(1 for result in evidence_results if result.created_evidence)
            deduplicated_count = len(evidence_results) - created_count
            version_before = checkpoint.version

            sync_page = ProviderSyncPage(
                user_id=user_id,
                provider_connection_id=connection_id,
                request_cursor=request_cursor,
                request_cursor_key=_cursor_key(request_cursor),
                next_cursor=page.next_cursor,
                has_more=page.has_more,
                page_hash=digest,
                observations_count=len(evidence_results),
                evidence_created=created_count,
                evidence_deduplicated=deduplicated_count,
                checkpoint_version_before=version_before,
                checkpoint_version_after=version_before + 1,
            )
            db.add(sync_page)
            db.flush()

            for ordinal, evidence_result in enumerate(evidence_results):
                db.add(
                    ProviderSyncPageEvidence(
                        sync_page_id=sync_page.id,
                        external_evidence_id=evidence_result.evidence.id,
                        ordinal=ordinal,
                        created_evidence=evidence_result.created_evidence,
                    )
                )

            checkpoint.committed_cursor = page.next_cursor
            checkpoint.version = version_before + 1
            checkpoint.updated_at = datetime.datetime.now(datetime.timezone.utc)
            db.flush()

            result = ProviderSyncPageCommit(
                page_id=sync_page.id,
                request_cursor=request_cursor,
                next_cursor=page.next_cursor,
                has_more=page.has_more,
                observations_count=len(evidence_results),
                evidence_created=created_count,
                evidence_deduplicated=deduplicated_count,
                checkpoint_version_after=checkpoint.version,
                replayed=False,
            )
        return result
    finally:
        db.close()


def read_committed_cursor(
    session_factory: Callable[[], Session],
    *,
    user_id: int,
    connection_id: int,
) -> str | None:
    db = session_factory()
    try:
        connection = (
            db.query(ProviderConnection)
            .filter(
                ProviderConnection.id == connection_id,
                ProviderConnection.user_id == user_id,
            )
            .one_or_none()
        )
        if connection is None:
            raise ProviderSyncError("Provider sync connection not found or ownership mismatch")
        checkpoint = get_sync_checkpoint(
            db,
            user_id=user_id,
            connection_id=connection_id,
        )
        return checkpoint.committed_cursor if checkpoint is not None else None
    finally:
        db.close()


def sync_provider_connection(
    session_factory: Callable[[], Session],
    *,
    user_id: int,
    connection_id: int,
    adapter: ProviderSyncAdapter,
    max_pages: int = 100,
) -> ProviderSyncResult:
    """Run an at-least-once, checkpointed provider sync.

    Fetches happen outside DB transactions. Each fetched page is committed atomically
    with its evidence and cursor. A worker/network crash therefore retries from the
    last committed cursor, and immutable evidence dedup makes those retries safe.
    """

    if max_pages < 1:
        raise ProviderSyncError("max_pages must be >= 1")

    start_cursor = read_committed_cursor(
        session_factory,
        user_id=user_id,
        connection_id=connection_id,
    )
    cursor = start_cursor
    pages_committed = 0
    pages_replayed = 0
    checkpoint_conflicts = 0
    evidence_created = 0
    evidence_deduplicated = 0

    for _ in range(max_pages):
        fetched = adapter.fetch_page(cursor)
        try:
            commit = commit_sync_page(
                session_factory,
                user_id=user_id,
                connection_id=connection_id,
                request_cursor=cursor,
                page=fetched,
            )
        except ProviderSyncCheckpointConflict as conflict:
            # Another worker safely won the page race. Never apply the stale fetched
            # page; restart provider fetch from the newly committed durable cursor.
            checkpoint_conflicts += 1
            cursor = conflict.actual_cursor
            continue

        if commit.replayed:
            pages_replayed += 1
        else:
            pages_committed += 1
            evidence_created += commit.evidence_created
            evidence_deduplicated += commit.evidence_deduplicated

        cursor = commit.next_cursor
        if not fetched.has_more:
            return ProviderSyncResult(
                connection_id=connection_id,
                start_cursor=start_cursor,
                end_cursor=cursor,
                pages_committed=pages_committed,
                pages_replayed=pages_replayed,
                checkpoint_conflicts=checkpoint_conflicts,
                evidence_created=evidence_created,
                evidence_deduplicated=evidence_deduplicated,
            )

    raise ProviderSyncError(
        f"Provider sync exceeded max_pages={max_pages}; refusing an unbounded worker loop"
    )
