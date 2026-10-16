import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.activity_feed_service import append_activity_event
from app.models import (
    ActivityFeedCursor,
    ActivityFeedEvent,
    User,
)


def new_user():
    with SessionLocal() as db:
        user = User(
            email=f"feed-{uuid.uuid4().hex}@example.com",
            hashed_password="test-only",
        )
        db.add(user)
        db.commit()
        return user.id


def test_feed_rollback_does_not_publish():
    user_id = new_user()

    with SessionLocal() as db:
        append_activity_event(
            db,
            user_id=user_id,
            event_type="financial.transaction.created",
            resource_type="transaction",
            resource_id=101,
        )
        db.rollback()

    with SessionLocal() as db:
        events = db.execute(
            select(ActivityFeedEvent)
            .where(ActivityFeedEvent.user_id == user_id)
        ).scalars().all()

        cursor = db.get(ActivityFeedCursor, user_id)

        assert events == []
        assert cursor is None


def test_concurrent_feed_appends_have_ordered_sequences():
    user_id = new_user()
    barrier = threading.Barrier(2)

    def worker(resource_id):
        barrier.wait(timeout=10)

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

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(worker, 201),
            pool.submit(worker, 202),
        ]
        sequences = [f.result(timeout=15) for f in futures]

    assert sorted(sequences) == [1, 2]

    with SessionLocal() as db:
        events = db.execute(
            select(ActivityFeedEvent)
            .where(ActivityFeedEvent.user_id == user_id)
            .order_by(ActivityFeedEvent.sequence)
        ).scalars().all()

        assert [e.sequence for e in events] == [1, 2]
        assert {e.resource_id for e in events} == {201, 202}


def test_second_transaction_waits_for_first_commit():
    user_id = new_user()

    first_appended = threading.Event()
    allow_first_commit = threading.Event()
    second_finished = threading.Event()

    def first():
        with SessionLocal() as db:
            append_activity_event(
                db,
                user_id=user_id,
                event_type="financial.transaction.created",
                resource_type="transaction",
                resource_id=301,
            )
            first_appended.set()

            assert allow_first_commit.wait(timeout=10)
            db.commit()

    def second():
        assert first_appended.wait(timeout=10)

        with SessionLocal() as db:
            append_activity_event(
                db,
                user_id=user_id,
                event_type="financial.transaction.created",
                resource_type="transaction",
                resource_id=302,
            )
            db.commit()
            second_finished.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(first)
        f2 = pool.submit(second)

        try:
            assert first_appended.wait(timeout=10)
            assert not second_finished.wait(timeout=0.2)
        finally:
            allow_first_commit.set()

        f1.result(timeout=15)
        f2.result(timeout=15)

    assert second_finished.is_set()

    with SessionLocal() as db:
        events = db.execute(
            select(ActivityFeedEvent)
            .where(ActivityFeedEvent.user_id == user_id)
            .order_by(ActivityFeedEvent.sequence)
        ).scalars().all()

        assert [e.sequence for e in events] == [1, 2]
        assert [e.resource_id for e in events] == [301, 302]


def test_rollback_releases_cursor_for_next_transaction():
    user_id = new_user()

    first_appended = threading.Event()
    allow_rollback = threading.Event()

    def first():
        with SessionLocal() as db:
            append_activity_event(
                db,
                user_id=user_id,
                event_type="financial.transaction.created",
                resource_type="transaction",
                resource_id=401,
            )
            first_appended.set()
            assert allow_rollback.wait(timeout=10)
            db.rollback()

    def second():
        assert first_appended.wait(timeout=10)
        with SessionLocal() as db:
            event = append_activity_event(
                db,
                user_id=user_id,
                event_type="financial.transaction.created",
                resource_type="transaction",
                resource_id=402,
            )
            sequence = event.sequence
            db.commit()
            return sequence

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(first)
        second_future = pool.submit(second)
        try:
            assert first_appended.wait(timeout=10)
            assert not second_future.done()
        finally:
            allow_rollback.set()

        first_future.result(timeout=15)
        sequence = second_future.result(timeout=15)

    assert sequence == 1

    with SessionLocal() as db:
        events = db.execute(
            select(ActivityFeedEvent)
            .where(ActivityFeedEvent.user_id == user_id)
            .order_by(ActivityFeedEvent.sequence)
        ).scalars().all()

        assert len(events) == 1
        assert events[0].sequence == 1
        assert events[0].resource_id == 402
