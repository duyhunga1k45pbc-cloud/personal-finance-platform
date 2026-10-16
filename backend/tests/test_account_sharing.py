from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.activity_feed_reader as activity_feed_reader
from app.authorization import Access, Action, ROUTE_POLICIES, ResourceType, RoutePolicy
from app.database import Base, SessionLocal, engine
from app.main import app
from app.models import (
    ProviderConnection,
    Transaction,
)
from app.routers.auth import Principal
from app.routers.gateway_authz import AuthorizationRequest, authorize

pytestmark = pytest.mark.security

client = TestClient(app)


@pytest.fixture(autouse=True)
def clean_test_database():
    assert engine.dialect.name == "postgresql", "account sharing tests require PostgreSQL"
    Base.metadata.create_all(bind=engine)
    tables = ", ".join(
        engine.dialect.identifier_preparer.quote(table.name)
        for table in Base.metadata.sorted_tables
    )
    with engine.begin() as connection:
        connection.exec_driver_sql(f"TRUNCATE TABLE {tables} RESTART IDENTITY CASCADE")
    yield


def _user(prefix: str) -> tuple[int, dict[str, str]]:
    email = f"{prefix}-{uuid.uuid4().hex}@example.com"
    password = "secret123"
    registered = client.post(
        "/auth/register",
        json={"email": email, "password": password},
    )
    assert registered.status_code == 200, registered.text
    login = client.post(
        "/auth/login",
        json={"email": email, "password": password},
    )
    assert login.status_code == 200, login.text
    return registered.json()["id"], {
        "Authorization": f"Bearer {login.json()['access_token']}",
    }


def _account(headers: dict[str, str]) -> dict:
    response = client.post(
        "/accounts",
        json={"name": "Shared account", "account_type": "BANK"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def _grant(headers: dict[str, str], account_id: int, grantee_user_id: int):
    return client.post(
        f"/accounts/{account_id}/shares",
        json={"grantee_user_id": grantee_user_id},
        headers=headers,
    )


def _command_headers(headers: dict[str, str]) -> dict[str, str]:
    return {**headers, "Idempotency-Key": uuid.uuid4().hex}


def test_owner_can_grant_list_and_revoke_viewer_access():
    owner_id, owner = _user("owner")
    viewer_id, viewer = _user("viewer")
    account = _account(owner)

    created = _grant(owner, account["id"], viewer_id)
    assert created.status_code == 201, created.text
    assert created.json()["account_id"] == account["id"]
    assert created.json()["grantee_user_id"] == viewer_id
    assert created.json()["role"] == "VIEWER"
    assert created.json()["created_at"]

    listed = client.get(f"/accounts/{account['id']}/shares", headers=owner)
    assert listed.status_code == 200
    assert [grant["grantee_user_id"] for grant in listed.json()] == [viewer_id]

    assert client.get(f"/accounts/{account['id']}/shares", headers=viewer).status_code == 404
    assert _grant(viewer, account["id"], owner_id).status_code == 404
    assert (
        client.delete(
            f"/accounts/{account['id']}/shares/{viewer_id}",
            headers=viewer,
        ).status_code
        == 404
    )

    revoked = client.delete(
        f"/accounts/{account['id']}/shares/{viewer_id}",
        headers=owner,
    )
    assert revoked.status_code == 204
    assert client.get(f"/accounts/{account['id']}/shares", headers=owner).json() == []
    assert client.get(f"/accounts/{account['id']}", headers=viewer).status_code == 404
    assert client.get("/accounts/shared-with-me", headers=viewer).json() == []


def test_shared_viewer_reads_only_explicitly_shared_account_metadata():
    owner_id, owner = _user("owner")
    viewer_id, viewer = _user("viewer")
    account = _account(owner)

    assert client.get("/accounts/shared-with-me", headers=viewer).json() == []
    assert _grant(owner, account["id"], viewer_id).status_code == 201

    account_response = client.get(f"/accounts/{account['id']}", headers=viewer)
    assert account_response.status_code == 200, account_response.text
    assert account_response.json()["user_id"] == owner_id

    shared = client.get("/accounts/shared-with-me", headers=viewer)
    assert shared.status_code == 200
    assert [item["id"] for item in shared.json()] == [account["id"]]
    owned = client.get("/accounts", headers=viewer)
    assert owned.status_code == 200
    assert account["id"] not in {item["id"] for item in owned.json()}


def test_duplicate_and_self_grants_are_rejected():
    owner_id, owner = _user("owner")
    viewer_id, _ = _user("viewer")
    account = _account(owner)

    assert _grant(owner, account["id"], viewer_id).status_code == 201
    duplicate = _grant(owner, account["id"], viewer_id)
    assert duplicate.status_code == 409
    assert len(client.get(f"/accounts/{account['id']}/shares", headers=owner).json()) == 1

    self_grant = _grant(owner, account["id"], owner_id)
    assert self_grant.status_code == 422
    assert client.get(f"/accounts/{account['id']}/shares", headers=owner).json()[0][
        "grantee_user_id"
    ] == viewer_id


def test_shared_read_still_requires_account_read_route_policy(monkeypatch):
    _, owner = _user("owner")
    viewer_id, viewer = _user("viewer")
    account = _account(owner)
    assert _grant(owner, account["id"], viewer_id).status_code == 201

    monkeypatch.setitem(
        ROUTE_POLICIES,
        ("GET", "/accounts/{account_id}"),
        RoutePolicy(Access.OWNER, ResourceType.TRANSACTION, Action.READ),
    )
    response = client.get(f"/accounts/{account['id']}", headers=viewer)
    assert response.status_code == 403


def test_non_owner_cannot_create_share_for_another_users_account():
    owner_id, owner = _user("owner")
    attacker_id, attacker = _user("attacker")
    target_id, _ = _user("target")
    account = _account(owner)

    response = _grant(attacker, account["id"], target_id)
    assert response.status_code == 404
    assert client.get(f"/accounts/{account['id']}/shares", headers=owner).json() == []
    assert attacker_id != owner_id


def test_viewer_cannot_read_or_write_owners_financial_data(monkeypatch):
    owner_id, owner = _user("owner")
    viewer_id, viewer = _user("viewer")
    account = _account(owner)
    assert _grant(owner, account["id"], viewer_id).status_code == 201

    transaction = client.post(
        "/transactions",
        json={
            "amount": "9876.00",
            "description": "owner transaction",
            "category": "test",
            "type": "income",
            "account_id": account["id"],
        },
        headers=_command_headers(owner),
    )
    assert transaction.status_code in (200, 201), transaction.text
    transaction_id = transaction.json()["id"]

    assert client.get(f"/transactions/{transaction_id}", headers=viewer).status_code == 404
    viewer_transactions = client.get("/transactions?limit=100", headers=viewer).json()
    assert transaction_id not in {item["id"] for item in viewer_transactions["data"]}
    viewer_summary = client.get("/summary", headers=viewer)
    assert viewer_summary.status_code == 200
    assert "9876" not in str(viewer_summary.json())

    assert client.post("/projections/rebuild", headers=viewer).status_code == 200
    projected_accounts = client.get("/projections/accounts", headers=viewer)
    assert projected_accounts.status_code == 200
    assert account["id"] not in {
        item["account_id"] for item in projected_accounts.json()
    }

    with SessionLocal() as db:
        connection = ProviderConnection(
            user_id=owner_id,
            provider_name="Test provider",
            external_account_id=uuid.uuid4().hex,
        )
        db.add(connection)
        db.commit()
        db.refresh(connection)
        connection_id = connection.id
    assert client.get(f"/provider-connections/{connection_id}", headers=viewer).status_code == 404

    denied_write = client.post(
        "/transactions",
        json={
            "amount": "1.00",
            "description": "viewer write",
            "category": "test",
            "type": "expense",
            "account_id": account["id"],
        },
        headers=_command_headers(viewer),
    )
    assert denied_write.status_code == 404
    assert denied_write.json()["detail"] == "Account not found"
    with SessionLocal() as db:
        assert db.query(Transaction).filter(Transaction.account_id == account["id"]).count() == 1

    test_engine = create_async_engine(activity_feed_reader._async_database_url())
    original_session_factory = activity_feed_reader.AsyncFeedSession
    with monkeypatch.context() as session_patch:
        session_patch.setattr(
            activity_feed_reader,
            "AsyncFeedSession",
            async_sessionmaker(bind=test_engine, expire_on_commit=False),
        )

        async def read_both_feeds():
            try:
                owner_rows = await activity_feed_reader.read_activity_events(
                    user_id=owner_id,
                    after_sequence=0,
                )
                viewer_rows = await activity_feed_reader.read_activity_events(
                    user_id=viewer_id,
                    after_sequence=0,
                )
                return owner_rows, viewer_rows
            finally:
                await test_engine.dispose()

        owner_events, viewer_events = asyncio.run(read_both_feeds())

    assert activity_feed_reader.AsyncFeedSession is original_session_factory
    assert [event["resource_id"] for event in owner_events] == [transaction_id]
    assert viewer_events == []


def test_unknown_grantee_and_unauthenticated_share_requests_are_denied():
    _, owner = _user("owner")
    account = _account(owner)
    missing_user = _grant(owner, account["id"], 2**31 - 1)
    assert missing_user.status_code == 404

    assert client.get("/accounts/shared-with-me").status_code == 401


def test_kong_account_pilot_routes_sharing_but_keeps_root_writes_blocked():
    principal = Principal(id=1, email="gateway-test@example.com")
    assert authorize(AuthorizationRequest(method="GET", path="/accounts"), principal)[
        "allow"
    ]
    assert not authorize(
        AuthorizationRequest(method="POST", path="/accounts"),
        principal,
    )["allow"]
    assert authorize(
        AuthorizationRequest(method="POST", path="/accounts/10/shares"),
        principal,
    )["allow"]
    assert authorize(
        AuthorizationRequest(method="GET", path="/accounts/shared-with-me"),
        principal,
    )["allow"]
    assert not authorize(
        AuthorizationRequest(method="POST", path="/accounts/10/shares/20"),
        principal,
    )["allow"]
    assert not authorize(
        AuthorizationRequest(method="GET", path="/transactions"),
        principal,
    )["allow"]
