from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import FinancialEvent, FinancialEventEntry
from app.routers.auth import (
    Principal,
    authorize_owner_principal,
    get_principal,
)


router = APIRouter(
    prefix="/financial-events",
    tags=["financial-events"],
    dependencies=[Depends(authorize_owner_principal)],
)

EventType = Literal[
    "INCOME", "EXPENSE", "TRANSFER",
    "REFUND", "REVERSAL", "ADJUSTMENT",
]


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.get("")
def list_financial_events(
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    event_type: EventType | None = Query(default=None),
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    # Authorization is enforced in SQL, not by frontend filtering.
    query = db.query(FinancialEvent).filter(
        FinancialEvent.user_id == principal.id
    )

    if event_type is not None:
        query = query.filter(
            FinancialEvent.event_type == event_type
        )

    total = query.count()

    events = (
        query
        .order_by(FinancialEvent.id.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )

    event_ids = [event.id for event in events]
    entries_by_event = {event_id: [] for event_id in event_ids}

    if event_ids:
        entries = (
            db.query(FinancialEventEntry)
            .filter(
                FinancialEventEntry.financial_event_id.in_(
                    event_ids
                )
            )
            .order_by(FinancialEventEntry.id.asc())
            .all()
        )

        for entry in entries:
            entries_by_event[entry.financial_event_id].append({
                "account_id": entry.account_id,
                "amount": format(Decimal(entry.amount), "f"),
            })

    data = []

    for event in events:
        data.append({
            "id": event.id,
            "event_type": event.event_type,
            "description": event.description,
            "category": event.category,
            "lifecycle_state": event.lifecycle_state,
            "version": event.version,
            "legacy_transaction_id": event.legacy_transaction_id,
            "occurred_at": event.occurred_at,
            "recorded_at": event.recorded_at,
            "entries": entries_by_event[event.id],
        })

    return {
        "total": total,
        "skip": skip,
        "limit": limit,
        "data": data,
    }
