from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.activity_feed_reader import (
    current_activity_cursor,
    read_activity_events,
)
from app.routers.auth import Principal, authorize_owner_principal, get_principal


router = APIRouter(
    prefix="/activity",
    tags=["activity"],
    dependencies=[Depends(authorize_owner_principal)],
)

POLL_INTERVAL = 0.5
HEARTBEAT_INTERVAL = 15.0


def parse_cursor(raw: str | None) -> int | None:
    if raw is None:
        return None
    if not raw.isascii() or not raw.isdecimal() or len(raw) > 19:
        raise HTTPException(status_code=400, detail="Invalid Last-Event-ID")
    cursor = int(raw)
    if cursor > 2**63 - 1:
        raise HTTPException(status_code=400, detail="Invalid Last-Event-ID")
    return cursor


@router.get("/stream")
async def stream_activity(
    request: Request,
    last_event_id: str | None = Header(
        default=None,
        alias="Last-Event-ID",
    ),
    principal: Principal = Depends(get_principal),
):
    cursor = parse_cursor(last_event_id)
    current = await current_activity_cursor(principal.id)

    if cursor is None:
        cursor = current
    elif cursor > current:
        raise HTTPException(status_code=400, detail="Cursor exceeds current feed")

    async def generate():
        nonlocal cursor
        last_heartbeat = asyncio.get_running_loop().time()

        while True:
            if await request.is_disconnected():
                return

            rows = await read_activity_events(
                user_id=principal.id,
                after_sequence=cursor,
                limit=100,
            )

            for row in rows:
                if await request.is_disconnected():
                    return

                sequence = row["sequence"]
                payload = json.dumps(row, separators=(",", ":"))

                yield (
                    f"id: {sequence}\n"
                    f"event: {row['event_type']}\n"
                    f"data: {payload}\n\n"
                )

                cursor = sequence

            now = asyncio.get_running_loop().time()
            if now - last_heartbeat >= HEARTBEAT_INTERVAL:
                yield ": heartbeat\n\n"
                last_heartbeat = now

            await asyncio.sleep(POLL_INTERVAL)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store, no-transform",
            "X-Accel-Buffering": "no",
        },
    )
