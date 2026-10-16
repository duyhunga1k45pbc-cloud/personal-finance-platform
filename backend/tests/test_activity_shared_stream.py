import asyncio
from types import SimpleNamespace

import pytest

from app.routers import activity_shared_stream as shared


@pytest.fixture(autouse=True)
def reset_shared_sse_state():
    """Isolate process-local lifecycle state across tests."""
    assert not shared._pollers, "Pollers leaked before test"
    previous = shared._draining
    shared._draining = False

    try:
        yield
    finally:
        assert not shared._pollers, "Pollers leaked after test"
        shared._draining = previous



class FakeRequest:
    async def is_disconnected(self):
        return False


class FakeFeed:
    def __init__(self):
        self.events = {}

    def commit(self, user_id, sequence):
        self.events.setdefault(user_id, []).append({
            "sequence": sequence,
            "event_type": "financial.transaction.created",
            "resource_type": "transaction",
            "resource_id": sequence,
        })

    async def cursor(self, user_id):
        rows = self.events.get(user_id, [])
        return rows[-1]["sequence"] if rows else 0

    async def read(self, *, user_id, after_sequence, limit=100):
        return [
            dict(row)
            for row in self.events.get(user_id, [])
            if row["sequence"] > after_sequence
        ][:limit]


def setup_feed(monkeypatch):
    feed = FakeFeed()
    monkeypatch.setattr(shared, "current_activity_cursor", feed.cursor)
    monkeypatch.setattr(shared, "read_activity_events", feed.read)
    monkeypatch.setattr(shared, "POLL_INTERVAL", 0.01)
    return feed


async def open_stream(user_id, cursor=None):
    response = await shared.shared_stream_activity(
        request=FakeRequest(),
        last_event_id=cursor,
        principal=SimpleNamespace(id=user_id),
    )
    return response.body_iterator


async def next_frame(iterator):
    return await asyncio.wait_for(anext(iterator), timeout=3)


async def close_stream(iterator):
    await iterator.aclose()


def test_reconnect_replays_in_order(monkeypatch):
    async def scenario():
        feed = setup_feed(monkeypatch)
        user_id = 101

        feed.commit(user_id, 1)
        feed.commit(user_id, 2)
        feed.commit(user_id, 3)

        stream = await open_stream(user_id, "1")

        try:
            first = await next_frame(stream)
            second = await next_frame(stream)

            assert "id: 2\n" in first
            assert "id: 3\n" in second

            feed.commit(user_id, 4)

            third = await next_frame(stream)
            assert "id: 4\n" in third
        finally:
            await close_stream(stream)

        assert user_id not in shared._pollers

    asyncio.run(scenario())


def test_commit_during_subscribe_not_lost(monkeypatch):
    async def scenario():
        feed = setup_feed(monkeypatch)
        user_id = 102

        feed.commit(user_id, 1)

        original_subscribe = shared._subscribe

        async def subscribe_with_concurrent_commit(uid):
            poller, queue = await original_subscribe(uid)

            # Simulate commit after subscribing,
            # but before the replay snapshot.
            feed.commit(uid, 2)

            return poller, queue

        monkeypatch.setattr(
            shared,
            "_subscribe",
            subscribe_with_concurrent_commit,
        )

        stream = await open_stream(user_id, "1")

        try:
            frame = await next_frame(stream)
            assert "id: 2\n" in frame

            feed.commit(user_id, 3)

            frame = await next_frame(stream)
            assert "id: 3\n" in frame
        finally:
            await close_stream(stream)

        assert user_id not in shared._pollers

    asyncio.run(scenario())


def test_user_isolation(monkeypatch):
    async def scenario():
        feed = setup_feed(monkeypatch)

        feed.commit(201, 1)
        feed.commit(202, 1)

        stream_a = await open_stream(201, "0")
        stream_b = await open_stream(202, "0")

        try:
            a = await next_frame(stream_a)
            b = await next_frame(stream_b)

            assert "id: 1\n" in a
            assert "id: 1\n" in b

            feed.commit(201, 2)

            a2 = await next_frame(stream_a)
            assert "id: 2\n" in a2

            # User B must not receive user A's event.
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(
                    anext(stream_b),
                    timeout=0.1,
                )
        finally:
            await close_stream(stream_a)
            await close_stream(stream_b)

        assert not shared._pollers

    asyncio.run(scenario())


def test_poller_failure_then_reconnect(monkeypatch):
    async def scenario():
        feed = setup_feed(monkeypatch)
        user_id = 301

        feed.commit(user_id, 1)

        original_read = feed.read
        fail_next = True

        async def flaky_read(**kwargs):
            nonlocal fail_next
            if fail_next:
                fail_next = False
                raise RuntimeError("Injected database failure")
            return await original_read(**kwargs)

        monkeypatch.setattr(shared, "read_activity_events", flaky_read)

        stream = await open_stream(user_id, "1")

        # First poll fails: server should end this stream.
        with pytest.raises(StopAsyncIteration):
            await next_frame(stream)

        await close_stream(stream)

        assert user_id not in shared._pollers

        # Event committed while disconnected.
        feed.commit(user_id, 2)

        stream2 = await open_stream(user_id, "1")
        try:
            frame = await next_frame(stream2)
            assert "id: 2\n" in frame
        finally:
            await close_stream(stream2)

    asyncio.run(scenario())


def test_queue_overflow_disconnects_slow_subscriber():
    async def scenario():
        poller = shared.UserPoller(user_id=401, cursor=0)
        queue = asyncio.Queue(maxsize=2)
        poller.subscribers.add(queue)

        await shared._publish(poller, {"sequence": 1})
        await shared._publish(poller, {"sequence": 2})
        await shared._publish(poller, {"sequence": 3})

        assert queue not in poller.subscribers
        assert await queue.get() is shared._STOP

    asyncio.run(scenario())


def test_last_subscriber_cancels_poller():
    async def scenario():
        user_id = 402
        queue = asyncio.Queue()

        async def poll_forever():
            await asyncio.sleep(100)

        poller = shared.UserPoller(user_id=user_id, cursor=0)
        poller.subscribers.add(queue)
        poller.task = asyncio.create_task(poll_forever())

        shared._pollers[user_id] = poller

        await shared._unsubscribe(poller, queue)

        assert user_id not in shared._pollers
        assert poller.task.done()
        assert poller.task.cancelled()

    asyncio.run(scenario())


def test_postgres_commit_during_subscribe_no_missing_event(monkeypatch):
    import uuid

    from app.activity_feed_service import append_activity_event
    from app.database import SessionLocal
    from app.models import User

    def create_user():
        with SessionLocal() as db:
            user = User(
                email=f"shared-race-{uuid.uuid4().hex}@example.com",
                hashed_password="test-only",
            )
            db.add(user)
            db.commit()
            return user.id

    def commit_event(user_id, resource_id):
        with SessionLocal() as db:
            event = append_activity_event(
                db,
                user_id=user_id,
                event_type="financial.transaction.created",
                resource_type="transaction",
                resource_id=resource_id,
            )
            sequence = event.sequence
            db.commit()
            return sequence

    async def scenario():
        user_id = await asyncio.to_thread(create_user)

        assert await asyncio.to_thread(
            commit_event, user_id, 801
        ) == 1

        original_subscribe = shared._subscribe

        async def subscribe_with_commit(uid):
            poller, queue = await original_subscribe(uid)

            # Real PostgreSQL COMMIT between registration
            # and the replay snapshot.
            sequence = await asyncio.to_thread(
                commit_event, uid, 802
            )
            assert sequence == 2

            return poller, queue

        monkeypatch.setattr(
            shared,
            "_subscribe",
            subscribe_with_commit,
        )

        monkeypatch.setattr(
            shared,
            "POLL_INTERVAL",
            0.02,
        )

        stream = await open_stream(user_id, "1")

        try:
            frame2 = await next_frame(stream)
            assert "id: 2\n" in frame2
            assert '"resource_id":802' in frame2

            # Another committed event after replay.
            assert await asyncio.to_thread(
                commit_event, user_id, 803
            ) == 3

            frame3 = await next_frame(stream)
            assert "id: 3\n" in frame3
            assert '"resource_id":803' in frame3

        finally:
            await close_stream(stream)

        assert user_id not in shared._pollers

    from app.activity_feed_reader import async_engine

    async def isolated_scenario():
        await async_engine.dispose()
        try:
            await scenario()
        finally:
            await async_engine.dispose()

    asyncio.run(isolated_scenario())


def test_postgres_commit_during_subscribe_no_missing_event(monkeypatch):
    import uuid

    from app.activity_feed_service import append_activity_event
    from app.database import SessionLocal
    from app.models import User

    def create_user():
        with SessionLocal() as db:
            user = User(
                email=f"shared-race-{uuid.uuid4().hex}@example.com",
                hashed_password="test-only",
            )
            db.add(user)
            db.commit()
            return user.id

    def commit_event(user_id, resource_id):
        with SessionLocal() as db:
            event = append_activity_event(
                db,
                user_id=user_id,
                event_type="financial.transaction.created",
                resource_type="transaction",
                resource_id=resource_id,
            )
            sequence = event.sequence
            db.commit()
            return sequence

    async def scenario():
        user_id = await asyncio.to_thread(create_user)

        assert await asyncio.to_thread(
            commit_event, user_id, 801
        ) == 1

        original_subscribe = shared._subscribe

        async def subscribe_with_commit(uid):
            poller, queue = await original_subscribe(uid)

            # Real PostgreSQL COMMIT between registration
            # and the replay snapshot.
            sequence = await asyncio.to_thread(
                commit_event, uid, 802
            )
            assert sequence == 2

            return poller, queue

        monkeypatch.setattr(
            shared,
            "_subscribe",
            subscribe_with_commit,
        )

        monkeypatch.setattr(
            shared,
            "POLL_INTERVAL",
            0.02,
        )

        stream = await open_stream(user_id, "1")

        try:
            frame2 = await next_frame(stream)
            assert "id: 2\n" in frame2
            assert '"resource_id":802' in frame2

            # Another committed event after replay.
            assert await asyncio.to_thread(
                commit_event, user_id, 803
            ) == 3

            frame3 = await next_frame(stream)
            assert "id: 3\n" in frame3
            assert '"resource_id":803' in frame3

        finally:
            await close_stream(stream)

        assert user_id not in shared._pollers

    from sqlalchemy.pool import NullPool
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from app.activity_feed_reader import _async_database_url
    import app.activity_feed_reader as reader

    # Dedicated engine: no pooled connection survives an asyncio.run() loop.
    test_engine = create_async_engine(
        _async_database_url(),
        poolclass=NullPool,
    )

    monkeypatch.setattr(
        reader,
        "AsyncFeedSession",
        async_sessionmaker(
            bind=test_engine,
            expire_on_commit=False,
        ),
    )

    async def isolated_scenario():
        try:
            await scenario()
        finally:
            await test_engine.dispose()

    asyncio.run(isolated_scenario())
