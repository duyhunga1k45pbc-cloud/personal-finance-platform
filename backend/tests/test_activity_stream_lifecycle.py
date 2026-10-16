import asyncio

from app.routers import activity_stream


def test_sse_generator_stops_after_cancellation(monkeypatch):
    asyncio.run(_check_sse_cancellation(monkeypatch))


async def _check_sse_cancellation(monkeypatch):
    polling_started = asyncio.Event()
    calls = 0

    async def fake_read_activity_events(*, user_id, after_sequence, limit):
        nonlocal calls
        calls += 1
        polling_started.set()
        return []

    async def fake_current_cursor(user_id):
        return 0

    class RequestStub:
        async def is_disconnected(self):
            return False

    class PrincipalStub:
        id = 123

    monkeypatch.setattr(
        activity_stream,
        "read_activity_events",
        fake_read_activity_events,
    )
    monkeypatch.setattr(
        activity_stream,
        "current_activity_cursor",
        fake_current_cursor,
    )
    monkeypatch.setattr(activity_stream, "POLL_INTERVAL", 0.01)

    response = await activity_stream.stream_activity(
        request=RequestStub(),
        last_event_id="0",
        principal=PrincipalStub(),
    )

    async def consume():
        async for _ in response.body_iterator:
            pass

    task = asyncio.create_task(consume())

    try:
        await asyncio.wait_for(polling_started.wait(), timeout=2)
        await asyncio.sleep(0.03)
        assert calls >= 1
    finally:
        task.cancel()

        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("SSE consumer was not cancelled")

    calls_after_cancel = calls
    await asyncio.sleep(0.05)

    assert calls == calls_after_cancel
