from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import ActivityFeedCursor, ActivityFeedEvent


def append_activity_event(
    db: Session,
    *,
    user_id: int,
    event_type: str,
    resource_type: str,
    resource_id: int,
) -> ActivityFeedEvent:
    """
    Append an event within the caller's existing transaction.

    The caller owns commit/rollback. No commit occurs here.

    The per-user cursor row is locked until transaction completion,
    ensuring that visible event sequences follow commit order.
    """

    # Safe if two transactions try to create a user's first cursor.
    stmt = (
        insert(ActivityFeedCursor)
        .values(user_id=user_id, last_sequence=0)
        .on_conflict_do_nothing(
            index_elements=[ActivityFeedCursor.user_id]
        )
    )
    db.execute(stmt)

    cursor = db.execute(
        select(ActivityFeedCursor)
        .where(ActivityFeedCursor.user_id == user_id)
        .with_for_update()
    ).scalar_one()

    cursor.last_sequence += 1
    db.flush()

    event = ActivityFeedEvent(
        user_id=user_id,
        sequence=cursor.last_sequence,
        event_type=event_type,
        resource_type=resource_type,
        resource_id=resource_id,
    )

    db.add(event)
    db.flush()

    return event
