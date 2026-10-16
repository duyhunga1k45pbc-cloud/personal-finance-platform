from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import ActivityFeedEvent


client = TestClient(app)


def test_transaction_create_emits_one_event_and_replay_emits_none():
    email = f"feed-api-{uuid4().hex}@example.com"
    password = "testpassword123"

    registered = client.post(
        "/auth/register",
        json={"email": email, "password": password},
    )
    assert registered.status_code == 200, registered.text
    user_id = registered.json()["id"]

    logged_in = client.post(
        "/auth/login",
        json={"email": email, "password": password},
    )
    assert logged_in.status_code == 200, logged_in.text

    token = logged_in.json()["access_token"]

    headers = {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": uuid4().hex,
    }

    payload = {
        "amount": "125000.50",
        "description": "activity feed test",
        "category": "income",
        "type": "income",
    }

    def events():
        with SessionLocal() as db:
            return db.execute(
                select(ActivityFeedEvent)
                .where(ActivityFeedEvent.user_id == user_id)
                .order_by(ActivityFeedEvent.sequence)
            ).scalars().all()

    # First command: create financial transaction + activity event.
    first = client.post(
        "/transactions",
        json=payload,
        headers=headers,
    )

    assert first.status_code == 200, first.text

    current_events = events()

    assert len(current_events) == 1
    assert current_events[0].sequence == 1
    assert current_events[0].event_type == "financial.transaction.created"
    assert current_events[0].resource_type == "transaction"
    assert current_events[0].resource_id == first.json()["id"]

    # Same key, same payload: replay without new activity.
    replay = client.post(
        "/transactions",
        json=payload,
        headers=headers,
    )

    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json()
    assert len(events()) == 1

    # Same key, different payload: conflict without new activity.
    conflicting_payload = {
        **payload,
        "amount": "999999.00",
    }

    conflict = client.post(
        "/transactions",
        json=conflicting_payload,
        headers=headers,
    )

    assert conflict.status_code == 409, conflict.text
    assert len(events()) == 1


def test_concurrent_same_key_transaction_emits_one_activity_event():
    from concurrent.futures import ThreadPoolExecutor
    from sqlalchemy import func

    from app.models import CommandReceipt, Transaction

    email = f"feed-race-{uuid4().hex}@example.com"
    password = "testpassword123"

    registered = client.post(
        "/auth/register",
        json={"email": email, "password": password},
    )
    assert registered.status_code == 200, registered.text
    user_id = registered.json()["id"]

    logged_in = client.post(
        "/auth/login",
        json={"email": email, "password": password},
    )
    assert logged_in.status_code == 200, logged_in.text

    headers = {
        "Authorization": f"Bearer {logged_in.json()['access_token']}",
        "Idempotency-Key": uuid4().hex,
    }

    payload = {
        "amount": "75000.00",
        "description": "concurrent activity feed test",
        "category": "income",
        "type": "income",
    }

    def send_request():
        return client.post(
            "/transactions",
            json=payload,
            headers=headers,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(send_request)
            for _ in range(2)
        ]
        responses = [future.result(timeout=15) for future in futures]

    assert all(r.status_code == 200 for r in responses), [
        (r.status_code, r.text) for r in responses
    ]

    assert responses[0].json() == responses[1].json()
    transaction_id = responses[0].json()["id"]

    with SessionLocal() as db:
        transactions = db.scalar(
            select(func.count())
            .select_from(Transaction)
            .where(
                Transaction.user_id == user_id,
                Transaction.id == transaction_id,
            )
        )

        receipts = db.scalar(
            select(func.count())
            .select_from(CommandReceipt)
            .where(
                CommandReceipt.user_id == user_id,
                CommandReceipt.command_type == "CREATE_TRANSACTION",
                CommandReceipt.idempotency_key == headers["Idempotency-Key"],
            )
        )

        events = db.execute(
            select(ActivityFeedEvent)
            .where(ActivityFeedEvent.user_id == user_id)
            .order_by(ActivityFeedEvent.sequence)
        ).scalars().all()

    assert transactions == 1
    assert receipts == 1
    assert len(events) == 1
    assert events[0].sequence == 1
    assert events[0].resource_id == transaction_id
