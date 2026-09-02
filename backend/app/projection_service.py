from __future__ import annotations

from dataclasses import dataclass
import datetime
from decimal import Decimal
import hashlib
import json

from sqlalchemy import func, text
from sqlalchemy.orm import Session, aliased

from app.canonical_service import FinancialSummary, canonical_summary
from app.models import (
    FinancialAccount,
    FinancialAccountBalanceProjection,
    FinancialEvent,
    FinancialEventEntry,
    FinancialEventLink,
    FinancialProjectionState,
    User,
)


class ProjectionError(RuntimeError):
    pass


class ProjectionMissingError(ProjectionError):
    pass


class ProjectionStaleError(ProjectionError):
    pass


@dataclass(frozen=True)
class ProjectionComputation:
    summary: FinancialSummary
    account_balances: dict[int, Decimal]
    net_worth: Decimal
    canonical_fingerprint: str


@dataclass(frozen=True)
class ProjectionStatus:
    status: str
    generation: int | None
    stored_fingerprint: str | None
    current_fingerprint: str
    rebuilt_at: datetime.datetime | None


def _decimal_text(value) -> str:
    return format(Decimal(value or 0), "f")


def canonical_projection_fingerprint(db: Session, user_id: int) -> str:
    """Hash every canonical input that can affect the Task 13 read model.

    The fingerprint is not the projection itself. It is a cheap correctness
    boundary for deciding whether a previously rebuilt projection still refers
    to the same canonical state. Event version is intentionally included, so a
    correction invalidates the read model even if the numeric amount happens
    to remain unchanged.
    """

    accounts = (
        db.query(
            FinancialAccount.id,
            FinancialAccount.account_type,
            FinancialAccount.currency,
        )
        .filter(FinancialAccount.user_id == user_id)
        .order_by(FinancialAccount.id.asc())
        .all()
    )
    events = (
        db.query(
            FinancialEvent.id,
            FinancialEvent.event_type,
            FinancialEvent.lifecycle_state,
            FinancialEvent.version,
        )
        .filter(FinancialEvent.user_id == user_id)
        .order_by(FinancialEvent.id.asc())
        .all()
    )
    event_ids = [row.id for row in events]

    if event_ids:
        entries = (
            db.query(
                FinancialEventEntry.id,
                FinancialEventEntry.financial_event_id,
                FinancialEventEntry.account_id,
                FinancialEventEntry.amount,
            )
            .filter(FinancialEventEntry.financial_event_id.in_(event_ids))
            .order_by(FinancialEventEntry.id.asc())
            .all()
        )
        from_event = aliased(FinancialEvent)
        links = (
            db.query(
                FinancialEventLink.id,
                FinancialEventLink.from_event_id,
                FinancialEventLink.to_event_id,
                FinancialEventLink.relation_type,
            )
            .join(from_event, from_event.id == FinancialEventLink.from_event_id)
            .filter(from_event.user_id == user_id)
            .order_by(FinancialEventLink.id.asc())
            .all()
        )
    else:
        entries = []
        links = []

    payload = {
        "accounts": [
            [row.id, row.account_type, row.currency]
            for row in accounts
        ],
        "events": [
            [row.id, row.event_type, row.lifecycle_state, row.version]
            for row in events
        ],
        "entries": [
            [
                row.id,
                row.financial_event_id,
                row.account_id,
                _decimal_text(row.amount),
            ]
            for row in entries
        ],
        "links": [
            [row.id, row.from_event_id, row.to_event_id, row.relation_type]
            for row in links
        ],
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def canonical_account_balances(db: Session, user_id: int) -> dict[int, Decimal]:
    accounts = (
        db.query(FinancialAccount.id)
        .filter(FinancialAccount.user_id == user_id)
        .order_by(FinancialAccount.id.asc())
        .all()
    )
    balances = {row.id: Decimal("0") for row in accounts}
    if not balances:
        return balances

    rows = (
        db.query(
            FinancialEventEntry.account_id,
            func.coalesce(func.sum(FinancialEventEntry.amount), 0).label("balance"),
        )
        .join(
            FinancialEvent,
            FinancialEvent.id == FinancialEventEntry.financial_event_id,
        )
        .filter(
            FinancialEvent.user_id == user_id,
            FinancialEvent.lifecycle_state == "ACTIVE",
            FinancialEventEntry.account_id.in_(list(balances)),
        )
        .group_by(FinancialEventEntry.account_id)
        .all()
    )
    for row in rows:
        balances[row.account_id] = Decimal(row.balance or 0)
    return balances


def compute_projection(db: Session, user_id: int) -> ProjectionComputation:
    user_exists = db.query(User.id).filter(User.id == user_id).scalar()
    if user_exists is None:
        raise ProjectionError("User not found")

    summary = canonical_summary(db, user_id)
    balances = canonical_account_balances(db, user_id)
    net_worth = sum(balances.values(), Decimal("0"))
    fingerprint = canonical_projection_fingerprint(db, user_id)
    return ProjectionComputation(
        summary=summary,
        account_balances=balances,
        net_worth=net_worth,
        canonical_fingerprint=fingerprint,
    )


def rebuild_user_projection(db: Session, user_id: int) -> FinancialProjectionState:
    """Delete and rebuild a user's read model in one DB transaction.

    Other transactions keep seeing the previous committed generation until this
    transaction commits. A failed rebuild rolls back both the delete and the new
    rows, so a partial generation is never published.
    """

    try:
        if db.get_bind().dialect.name == "postgresql":
            # All canonical reads in this rebuild must refer to one source
            # snapshot. A concurrent canonical write may make the new projection
            # stale immediately after commit, which the fingerprint detects, but
            # it cannot make this generation internally mixed.
            db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))

        computed = compute_projection(db, user_id)
        state = (
            db.query(FinancialProjectionState)
            .filter(FinancialProjectionState.user_id == user_id)
            .with_for_update()
            .one_or_none()
        )
        generation = 1 if state is None else state.generation + 1
        rebuilt_at = datetime.datetime.now(datetime.timezone.utc)

        db.query(FinancialAccountBalanceProjection).filter(
            FinancialAccountBalanceProjection.user_id == user_id
        ).delete(synchronize_session=False)

        for account_id, balance in sorted(computed.account_balances.items()):
            db.add(
                FinancialAccountBalanceProjection(
                    user_id=user_id,
                    account_id=account_id,
                    generation=generation,
                    balance=balance,
                    rebuilt_at=rebuilt_at,
                )
            )

        if state is None:
            state = FinancialProjectionState(user_id=user_id)
            db.add(state)

        state.generation = generation
        state.canonical_fingerprint = computed.canonical_fingerprint
        state.total_income = computed.summary.total_income
        state.total_expense = computed.summary.total_expense
        state.economic_balance = computed.summary.balance
        state.net_worth = computed.net_worth
        state.account_count = len(computed.account_balances)
        state.rebuilt_at = rebuilt_at

        db.flush()
        db.commit()
        db.refresh(state)
        return state
    except Exception:
        db.rollback()
        raise


def projection_status(db: Session, user_id: int) -> ProjectionStatus:
    current = canonical_projection_fingerprint(db, user_id)
    state = (
        db.query(FinancialProjectionState)
        .filter(FinancialProjectionState.user_id == user_id)
        .one_or_none()
    )
    if state is None:
        return ProjectionStatus(
            status="MISSING",
            generation=None,
            stored_fingerprint=None,
            current_fingerprint=current,
            rebuilt_at=None,
        )
    status = "FRESH" if state.canonical_fingerprint == current else "STALE"
    return ProjectionStatus(
        status=status,
        generation=state.generation,
        stored_fingerprint=state.canonical_fingerprint,
        current_fingerprint=current,
        rebuilt_at=state.rebuilt_at,
    )


def require_fresh_projection(db: Session, user_id: int) -> FinancialProjectionState:
    status = projection_status(db, user_id)
    if status.status == "MISSING":
        raise ProjectionMissingError("Projection has not been rebuilt")
    if status.status == "STALE":
        raise ProjectionStaleError("Projection is stale relative to canonical state")
    return (
        db.query(FinancialProjectionState)
        .filter(FinancialProjectionState.user_id == user_id)
        .one()
    )
