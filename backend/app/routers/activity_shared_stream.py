from __future__ import annotations

import asyncio
import contextlib
import logging
import json
import os
from pathlib import Path
from dataclasses import dataclass, field

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.activity_feed_reader import (
    current_activity_cursor,
    read_activity_events,
)
from app.routers.activity_stream import parse_cursor
from app.routers.auth import (
    Principal,
    authorize_owner_principal,
    get_principal,
)

logger = logging.getLogger("pfp.sse.shared")


router = APIRouter(
    prefix="/activity",
    tags=["activity"],
    dependencies=[Depends(authorize_owner_principal)],
)

POLL_INTERVAL = 0.5
QUEUE_SIZE = 256
HEARTBEAT_INTERVAL = 15.0

_STOP = object()


@dataclass
class UserPoller:
    user_id: int
    cursor: int
    subscribers: set[asyncio.Queue] = field(default_factory=set)
    task: asyncio.Task | None = None


_pollers: dict[int, UserPoller] = {}
_registry_lock = asyncio.Lock()
_draining = False


async def _publish(poller: UserPoller, event: dict):
    for queue in tuple(poller.subscribers):
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            # A slow subscriber must reconnect and replay from its cursor.
            poller.subscribers.discard(queue)
            while not queue.empty():
                queue.get_nowait()
            queue.put_nowait(_STOP)


async def _run_poller(poller: UserPoller):
    try:
        while True:
            rows = await read_activity_events(
                user_id=poller.user_id,
                after_sequence=poller.cursor,
                limit=100,
            )

            for row in rows:
                await _publish(poller, row)
                poller.cursor = row["sequence"]

            # Drain backlog without unnecessary sleep.
            if len(rows) == 100:
                continue

            await asyncio.sleep(POLL_INTERVAL)

    except asyncio.CancelledError:
        raise
    except Exception:
        # On reader failure, close subscribers rather than hang forever.
        for queue in tuple(poller.subscribers):
            poller.subscribers.discard(queue)
            while not queue.empty():
                queue.get_nowait()
            queue.put_nowait(_STOP)

        logger.exception("Shared SSE poller failed: user_id=%s", poller.user_id)
        return


async def _subscribe(user_id: int):
    async with _registry_lock:
        if _draining:
            raise RuntimeError("Shared SSE is draining")
        poller = _pollers.get(user_id)

        if poller is None or poller.task is None or poller.task.done():
            # Establish the polling baseline before registering clients.
            cursor = await current_activity_cursor(user_id)
            poller = UserPoller(user_id=user_id, cursor=cursor)
            _pollers[user_id] = poller

        queue = asyncio.Queue(maxsize=QUEUE_SIZE)
        poller.subscribers.add(queue)

        if poller.task is None:
            poller.task = asyncio.create_task(_run_poller(poller))

        return poller, queue


async def _unsubscribe(poller: UserPoller, queue: asyncio.Queue):
    task_to_cancel = None

    async with _registry_lock:
        poller.subscribers.discard(queue)

        if not poller.subscribers:
            if _pollers.get(poller.user_id) is poller:
                del _pollers[poller.user_id]

            task_to_cancel = poller.task
            if task_to_cancel is not None:
                task_to_cancel.cancel()

    # Never wait for a task while holding the registry lock.
    if task_to_cancel is not None:
        with contextlib.suppress(asyncio.CancelledError):
            await task_to_cancel



async def shutdown_shared_pollers():
    """Stop all in-process shared pollers before disposing the DB engine."""
    global _draining

    async with _registry_lock:
        _draining = True
        pollers = list(_pollers.values())
        _pollers.clear()

        for poller in pollers:
            for queue in tuple(poller.subscribers):
                poller.subscribers.discard(queue)

                while not queue.empty():
                    queue.get_nowait()

                queue.put_nowait(_STOP)

            if poller.task is not None:
                poller.task.cancel()

    tasks = [
        poller.task
        for poller in pollers
        if poller.task is not None
    ]

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)



async def reset_shared_sse_for_startup():
    """Reset draining state for a new application lifecycle."""
    global _draining

    async with _registry_lock:
        if _pollers:
            raise RuntimeError("Cannot reset SSE with active pollers")
        _draining = False


async def watch_drain_marker():
    """Optional local-only pre-drain watcher."""
    marker = os.getenv("PFP_SSE_DRAIN_MARKER", "").strip()

    if not marker:
        return

    path = Path(marker)

    # A stale marker must not silently drain a fresh worker.
    if path.exists():
        raise RuntimeError(
            "SSE drain marker already exists at startup"
        )

    logger.info("SSE drain watcher started")

    while True:
        if path.exists():
            logger.info("SSE pre-drain requested")
            from app.deployment import mark_process_draining

            mark_process_draining()
            await shutdown_shared_pollers()
            logger.info("SSE pre-drain completed")
            return

        await asyncio.sleep(0.2)


def _format_event(row: dict) -> str:
    payload = json.dumps(row, separators=(",", ":"))
    return (
        f"id: {row['sequence']}\n"
        f"event: {row['event_type']}\n"
        f"data: {payload}\n\n"
    )


@router.get("/shared-stream")
async def shared_stream_activity(
    request: Request,
    last_event_id: str | None = Header(
        default=None,
        alias="Last-Event-ID",
    ),
    principal: Principal = Depends(get_principal),
):
    requested_cursor = parse_cursor(last_event_id)
    current = await current_activity_cursor(principal.id)

    if requested_cursor is not None and requested_cursor > current:
        raise HTTPException(
            status_code=400,
            detail="Cursor exceeds current feed",
        )

    async with _registry_lock:
        if _draining:
            raise HTTPException(
                status_code=503,
                detail="Shared SSE is draining",
                headers={"Retry-After": "1"},
            )

    async def generate():
        try:
            poller, queue = await _subscribe(principal.id)
        except RuntimeError as exc:
            if str(exc) == "Shared SSE is draining":
                return
            raise

        try:
            # Subscribe FIRST, then snapshot the committed cursor.
            # Events arriving during replay are buffered in the queue.
            snapshot = await current_activity_cursor(principal.id)

            cursor = (
                snapshot
                if requested_cursor is None
                else requested_cursor
            )

            # Replay persisted events through the snapshot.
            while cursor < snapshot:
                rows = await read_activity_events(
                    user_id=principal.id,
                    after_sequence=cursor,
                    limit=100,
                )

                if not rows:
                    break

                for row in rows:
                    if row["sequence"] > snapshot:
                        break
                    yield _format_event(row)
                    cursor = row["sequence"]

            while True:
                if await request.is_disconnected():
                    return

                try:
                    item = await asyncio.wait_for(
                        queue.get(),
                        timeout=HEARTBEAT_INTERVAL,
                    )
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
                    continue

                if item is _STOP:
                    return

                # Queue may include events already replayed.
                if item["sequence"] <= cursor:
                    continue

                yield _format_event(item)
                cursor = item["sequence"]

        finally:
            await _unsubscribe(poller, queue)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store, no-transform",
            "X-Accel-Buffering": "no",
        },
    )
