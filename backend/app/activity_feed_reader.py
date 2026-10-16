from __future__ import annotations

import os
import logging
import time

from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import ActivityFeedEvent, ActivityFeedCursor

logger = logging.getLogger("pfp.sse.metrics")


def _async_database_url() -> str:
    url = make_url(os.environ["DATABASE_URL"])
    if url.get_backend_name() != "postgresql":
        raise RuntimeError("SSE reader requires PostgreSQL")
    return url.set(drivername="postgresql+asyncpg").render_as_string(
        hide_password=False
    )


async_engine = create_async_engine(
    _async_database_url(),
    pool_size=20,
    max_overflow=10,
    pool_pre_ping=True,
)

AsyncFeedSession = async_sessionmaker(
    bind=async_engine,
    expire_on_commit=False,
)


async def read_activity_events(
    *,
    user_id: int,
    after_sequence: int,
    limit: int = 100,
) -> list[dict]:
    started = time.perf_counter()

    async with AsyncFeedSession() as db:
        conn_started = time.perf_counter()

        await db.connection()
        acquired = time.perf_counter()

        result = await db.execute(
            select(
                ActivityFeedEvent.sequence,
                ActivityFeedEvent.event_type,
                ActivityFeedEvent.resource_type,
                ActivityFeedEvent.resource_id,
            )
            .where(
                ActivityFeedEvent.user_id == user_id,
                ActivityFeedEvent.sequence > after_sequence,
            )
            .order_by(ActivityFeedEvent.sequence.asc())
            .limit(limit)
        )

        executed = time.perf_counter()

        rows = [
            {
                "sequence": row.sequence,
                "event_type": row.event_type,
                "resource_type": row.resource_type,
                "resource_id": row.resource_id,
            }
            for row in result
        ]

    finished = time.perf_counter()

    acquire_ms = (acquired - conn_started) * 1000
    execute_ms = (executed - acquired) * 1000
    total_ms = (finished - started) * 1000

    if total_ms >= 100:
        logger.warning(
            "sse_read acquire_ms=%.2f execute_ms=%.2f total_ms=%.2f",
            acquire_ms,
            execute_ms,
            total_ms,
        )

    return rows


async def current_activity_cursor(user_id: int) -> int:
    async with AsyncFeedSession() as db:
        result = await db.execute(
            select(ActivityFeedCursor.last_sequence)
            .where(ActivityFeedCursor.user_id == user_id)
        )
        return result.scalar_one_or_none() or 0
