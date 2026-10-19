import json
import os
import queue
import threading
import time
import uuid

import pytest
import requests
from sqlalchemy import create_engine, delete, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.activity_feed_service import append_activity_event
from app.models import ActivityFeedCursor, ActivityFeedEvent

BASE = "http://127.0.0.1:18004"
TOKEN_URL = (
    "http://127.0.0.1:18081/realms/pfp-e2e/"
    "protocol/openid-connect/token"
)

DB_URL = os.environ["E2E_DATABASE_URL"]
parsed = make_url(DB_URL)

assert parsed.database == "finance_e2e_db"
assert parsed.host in ("127.0.0.1", "localhost")
assert parsed.port == 15432

engine = create_engine(DB_URL)


def get_token(username, password):
    response = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "password",
            "client_id": "moneta",
            "username": username,
            "password": password,
        },
        timeout=15,
    )
    response.raise_for_status()
    return response.json()["access_token"]


@pytest.fixture
def principals():
    result = {}

    for label, username, password in (
        ("A", "e2e-user", "e2e-test-password"),
        ("B", "e2e-user-b", "e2e-b-test-password"),
    ):
        token = get_token(username, password)
        headers = {"Authorization": f"Bearer {token}"}

        response = requests.get(
            BASE + "/auth/me",
            headers=headers,
            timeout=15,
        )
        response.raise_for_status()

        result[label] = {
            "id": response.json()["id"],
            "headers": headers,
        }

    assert result["A"]["id"] != result["B"]["id"]
    return result


def check(label, method, path, headers, status, **kwargs):
    response = requests.request(
        method,
        BASE + path,
        headers=headers,
        timeout=15,
        **kwargs,
    )

    assert response.status_code == status, (
        f"{label}: expected {status}, "
        f"got {response.status_code}: {response.text[:400]}"
    )

    print(f"{label}: PASS")
    return response


def test_account_grant_revoke_e2e(principals):
    a = principals["A"]
    b = principals["B"]
    account_id = None

    try:
        with engine.begin() as conn:
            account_id = conn.execute(
                text(
                """
                INSERT INTO financial_accounts
                    (user_id, name, account_type,
                     currency, is_default, version)
                VALUES
                    (:owner, 'E2E Regression Fixture',
                     'BANK', 'VND', false, 1)
                RETURNING id
                """),
                {"owner": a["id"]},
            ).scalar_one()

        path = f"/accounts/{account_id}"

        check("Owner read", "GET", path, a["headers"], 200)
        check("Unauthorized read", "GET", path, b["headers"], 404)

        check(
            "Grant VIEWER",
            "POST",
            path + "/shares",
            a["headers"],
            201,
            json={"grantee_user_id": b["id"]},
        )

        check("Viewer read", "GET", path, b["headers"], 200)

        check(
            "Viewer cannot delegate",
            "POST",
            path + "/shares",
            b["headers"],
            404,
            json={"grantee_user_id": a["id"]},
        )

        check(
            "Revoke VIEWER",
            "DELETE",
            path + f"/shares/{b['id']}",
            a["headers"],
            204,
        )

        check("Read after revoke", "GET", path, b["headers"], 404)

    finally:
        if account_id is not None:
            with engine.begin() as conn:
                conn.execute(
                    text("DELETE FROM account_grants WHERE account_id = :id"),
                    {"id": account_id},
                )
                conn.execute(
                    text("DELETE FROM financial_accounts WHERE id = :id"),
                    {"id": account_id},
                )


def test_concurrent_sse_isolation_e2e(principals):
    user_ids = [principals["A"]["id"], principals["B"]["id"]]
    marker = "E2E_LIVE_" + uuid.uuid4().hex[:8]

    messages = {uid: queue.Queue() for uid in user_ids}
    streams = []
    threads = []
    created = []
    previous = {}

    def consume(uid, response):
        try:
            for line in response.iter_lines(
                chunk_size=1,
                decode_unicode=True,
            ):
                if line.startswith("data:"):
                    messages[uid].put(json.loads(line[5:].strip()))
        except Exception as exc:
            messages[uid].put(exc)

    def publish(stage):
        pending = []

        with Session(engine) as db:
            for position, uid in enumerate(user_ids, start=1):
                event = append_activity_event(
                    db,
                    user_id=uid,
                    event_type=marker,
                    resource_type="E2E_TEST",
                    resource_id=stage * 10000 + position,
                )
                pending.append((uid, event.sequence))

            db.commit()

        created.extend(pending)

    def expect(uid, resource_id):
        deadline = time.monotonic() + 15

        while time.monotonic() < deadline:
            try:
                item = messages[uid].get(
                    timeout=max(0.01, deadline - time.monotonic())
                )
            except queue.Empty:
                break

            if isinstance(item, Exception):
                raise item

            if item.get("event_type") != marker:
                continue

            assert item["resource_id"] == resource_id, (
                f"Cross-user event received: {item}"
            )
            return

        pytest.fail(f"Missing SSE event for user {uid}")

    try:
        with Session(engine) as db:
            for uid in user_ids:
                cursor = db.get(ActivityFeedCursor, uid)
                previous[uid] = cursor.last_sequence if cursor else 0

        for label in ("A", "B"):
            uid = principals[label]["id"]
            headers = {
                **principals[label]["headers"],
                "Last-Event-ID": str(previous[uid]),
                "Accept": "text/event-stream",
            }

            response = requests.get(
                BASE + "/activity/shared-stream",
                headers=headers,
                stream=True,
                timeout=(5, 20),
            )

            assert response.status_code == 200
            streams.append(response)

            thread = threading.Thread(
                target=consume,
                args=(uid, response),
                daemon=True,
            )
            thread.start()
            threads.append(thread)

        publish(1)
        expect(user_ids[0], 10001)
        expect(user_ids[1], 10002)

        publish(2)
        expect(user_ids[0], 20001)
        expect(user_ids[1], 20002)

        print("CONCURRENT SSE ISOLATION: PASS")

    finally:
        for response in streams:
            response.close()

        for thread in threads:
            thread.join(timeout=2)

        with Session(engine) as db:
            for uid, seq in created:
                db.execute(
                    delete(ActivityFeedEvent).where(
                        ActivityFeedEvent.user_id == uid,
                        ActivityFeedEvent.sequence == seq,
                        ActivityFeedEvent.event_type == marker,
                    )
                )

            for uid, old in previous.items():
                own = [seq for user, seq in created if user == uid]
                if not own:
                    continue

                cursor = db.execute(
                    select(ActivityFeedCursor)
                    .where(ActivityFeedCursor.user_id == uid)
                    .with_for_update()
                ).scalar_one_or_none()

                if cursor and cursor.last_sequence == max(own):
                    if old == 0:
                        db.delete(cursor)
                    else:
                        cursor.last_sequence = old

            db.commit()
