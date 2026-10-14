"""Authorization architecture tests (PostgreSQL-backed, adversarial).

Attacker ("mallory") calls every user-scoped endpoint with the victim's
("alice") identifiers. Denied calls must keep the existing 404 contract and
leave the whole database byte-for-byte unchanged.
"""

from __future__ import annotations

import datetime
import uuid

import pytest
from fastapi.testclient import TestClient
from fastapi.routing import APIRoute
from sqlalchemy import event, text

from app import authorization as authz
from app.authorization import Action, ROUTE_POLICIES, Access, ResourceType, RoutePolicy
from app.database import Base, SessionLocal, engine
from app.main import app
from app.routers import (
    accounts,
    auth,
    causal_events,
    gateway_authz,
    observability,
    projections,
    providers,
    reconciliations,
    transactions,
    transfers,
)

pytestmark = pytest.mark.security

client = TestClient(app)

ROUTERS = [
    auth.router,
    accounts.router,
    transactions.router,
    transfers.router,
    causal_events.router,
    reconciliations.router,
    observability.router,
    projections.router,
    providers.router,
    gateway_authz.router,
]

# Independently reviewed route contract. Keep this explicit rather than
# constructing it from ROUTE_POLICIES so policy drift is detectable.
EXPECTED_OWNER_ROUTE_CONTRACT = {
    ("GET", "/summary"): (ResourceType.SUMMARY, Action.READ),
    ("GET", "/auth/me"): (ResourceType.USER, Action.READ),
    ("POST", "/internal/gateway/authorize"): (ResourceType.USER, Action.EXECUTE),
    ("GET", "/accounts"): (ResourceType.ACCOUNT, Action.LIST),
    ("POST", "/accounts"): (ResourceType.ACCOUNT, Action.CREATE),
    ("GET", "/accounts/{account_id}"): (ResourceType.ACCOUNT, Action.READ),
    ("GET", "/transactions"): (ResourceType.TRANSACTION, Action.LIST),
    ("GET", "/transactions/{transaction_id}"): (ResourceType.TRANSACTION, Action.READ),
    ("GET", "/transactions/{transaction_id}/history"): (ResourceType.TRANSACTION, Action.READ),
    ("POST", "/transactions"): (ResourceType.TRANSACTION, Action.CREATE),
    ("PUT", "/transactions/{transaction_id}"): (ResourceType.TRANSACTION, Action.UPDATE),
    ("DELETE", "/transactions/{transaction_id}"): (ResourceType.TRANSACTION, Action.DELETE),
    ("POST", "/transfers"): (ResourceType.TRANSFER, Action.CREATE),
    ("POST", "/refunds"): (ResourceType.CAUSAL_EVENT, Action.CREATE),
    ("POST", "/reversals"): (ResourceType.CAUSAL_EVENT, Action.CREATE),
    ("GET", "/reconciliations/{reconciliation_id}"): (
        ResourceType.RECONCILIATION,
        Action.READ,
    ),
    ("POST", "/reconciliations"): (ResourceType.RECONCILIATION, Action.CREATE),
    ("POST", "/reconciliations/{reconciliation_id}/resolve"): (
        ResourceType.RECONCILIATION,
        Action.UPDATE,
    ),
    ("POST", "/reconciliations/{reconciliation_id}/adjustments"): (
        ResourceType.RECONCILIATION,
        Action.UPDATE,
    ),
    ("POST", "/projections/rebuild"): (ResourceType.PROJECTION, Action.EXECUTE),
    ("GET", "/projections/status"): (ResourceType.PROJECTION, Action.READ),
    ("GET", "/projections/summary"): (ResourceType.PROJECTION, Action.READ),
    ("GET", "/projections/accounts"): (ResourceType.PROJECTION, Action.LIST),
    ("POST", "/provider-connections"): (
        ResourceType.PROVIDER_CONNECTION,
        Action.CREATE,
    ),
    ("GET", "/provider-connections"): (ResourceType.PROVIDER_CONNECTION, Action.LIST),
    ("GET", "/provider-connections/{connection_id}"): (
        ResourceType.PROVIDER_CONNECTION,
        Action.READ,
    ),
    ("GET", "/provider-connections/{connection_id}/sync-checkpoint"): (
        ResourceType.PROVIDER_CONNECTION,
        Action.READ,
    ),
    ("GET", "/provider-connections/{connection_id}/sync-pages"): (
        ResourceType.PROVIDER_CONNECTION,
        Action.LIST,
    ),
    ("POST", "/provider-connections/{connection_id}/external-transactions"): (
        ResourceType.EXTERNAL_TRANSACTION,
        Action.CREATE,
    ),
    ("GET", "/provider-connections/{connection_id}/external-transactions"): (
        ResourceType.EXTERNAL_TRANSACTION,
        Action.LIST,
    ),
    (
        "GET",
        "/provider-connections/{connection_id}/external-transactions/"
        "{transaction_record_id}/evidence",
    ): (ResourceType.EXTERNAL_TRANSACTION, Action.READ),
    (
        "POST",
        "/provider-connections/{connection_id}/external-transactions/"
        "{transaction_record_id}/normalizations",
    ): (ResourceType.EXTERNAL_TRANSACTION, Action.EXECUTE),
    (
        "GET",
        "/provider-connections/{connection_id}/external-transactions/"
        "{transaction_record_id}/interpretation",
    ): (ResourceType.EXTERNAL_TRANSACTION, Action.READ),
    (
        "GET",
        "/provider-connections/{connection_id}/external-transactions/"
        "{transaction_record_id}/lifecycle",
    ): (ResourceType.EXTERNAL_TRANSACTION, Action.READ),
    (
        "POST",
        "/provider-connections/{connection_id}/external-transactions/"
        "{transaction_record_id}/classification",
    ): (ResourceType.EXTERNAL_TRANSACTION, Action.UPDATE),
    (
        "POST",
        "/provider-connections/{connection_id}/external-transactions/"
        "{transaction_record_id}/confirm",
    ): (ResourceType.EXTERNAL_TRANSACTION, Action.UPDATE),
}

EXPECTED_PUBLIC_ROUTES = {
    ("GET", "/"),
    ("GET", "/demo"),
    ("POST", "/auth/register"),
    ("POST", "/auth/login"),
    ("GET", "/health/live"),
    ("GET", "/health/ready"),
}
EXPECTED_METRICS_ROUTES = {("GET", "/observability/metrics")}


@pytest.fixture(autouse=True)
def postgres_only_and_clean():
    assert engine.dialect.name == "postgresql", "authorization tests require PostgreSQL"
    Base.metadata.create_all(bind=engine)
    names = ", ".join(
        engine.dialect.identifier_preparer.quote(t.name)
        for t in Base.metadata.sorted_tables
    )
    with engine.begin() as conn:
        conn.exec_driver_sql(f"TRUNCATE TABLE {names} RESTART IDENTITY CASCADE")
    yield


# ---------------------------------------------------------------- policy model


def _all_routes():
    routes = [r for r in app.routes if isinstance(r, APIRoute)]
    for router in ROUTERS:
        routes.extend(r for r in router.routes if isinstance(r, APIRoute))
    return routes


def _route_keys(route):
    return [(m, route.path) for m in sorted(route.methods - {"HEAD", "OPTIONS"})]


def _dependency_names(route):
    names = set()

    def walk(dep):
        for sub in dep.dependencies:
            names.add(getattr(sub.call, "__name__", ""))
            walk(sub)

    walk(route.dependant)
    return names


def _assert_route_contract(policies, route_keys):
    expected_owner_keys = set(EXPECTED_OWNER_ROUTE_CONTRACT)
    expected_keys = (
        expected_owner_keys | EXPECTED_PUBLIC_ROUTES | EXPECTED_METRICS_ROUTES
    )
    assert set(route_keys) == expected_keys, (
        f"route inventory mismatch; missing={expected_keys - set(route_keys)}, "
        f"unexpected={set(route_keys) - expected_keys}"
    )
    assert set(policies) == expected_keys, (
        f"policy inventory mismatch; missing={expected_keys - set(policies)}, "
        f"unexpected={set(policies) - expected_keys}"
    )

    for key, expected in EXPECTED_OWNER_ROUTE_CONTRACT.items():
        policy = policies[key]
        assert policy.access is Access.OWNER, key
        assert (policy.resource, policy.action) == expected, key

    for key in EXPECTED_PUBLIC_ROUTES:
        policy = policies[key]
        assert policy.access is Access.PUBLIC, key
        assert policy.resource is None and policy.action is None, key

    for key in EXPECTED_METRICS_ROUTES:
        policy = policies[key]
        assert policy.access is Access.METRICS_TOKEN, key
        assert policy.resource is None and policy.action is None, key


def test_every_route_declares_a_policy_and_is_guarded_accordingly():
    undeclared, unguarded = [], []
    seen = set()
    for route in _all_routes():
        deps = _dependency_names(route)
        for key in _route_keys(route):
            seen.add(key)
            policy = ROUTE_POLICIES.get(key)
            if policy is None:
                undeclared.append(key)
            elif policy.access is Access.OWNER and not ({"get_current_user", "get_principal"} & deps):
                unguarded.append(key)
            elif policy.access is Access.METRICS_TOKEN and "authorize_metrics" not in deps:
                unguarded.append(key)
            elif policy.access is Access.PUBLIC and ({"get_current_user", "get_principal"} & deps):
                unguarded.append(key)  # stale policy: declared public but guarded
    assert not undeclared, f"routes without policy: {undeclared}"
    assert not unguarded, f"routes whose guard disagrees with policy: {unguarded}"
    assert set(ROUTE_POLICIES) == seen, "policy table has stale entries"


def test_independent_route_contract_matches_policies_and_route_inventory():
    route_keys = {
        key
        for route in _all_routes()
        for key in _route_keys(route)
    }
    assert len(EXPECTED_OWNER_ROUTE_CONTRACT) == 36
    _assert_route_contract(ROUTE_POLICIES, route_keys)


def test_route_contract_rejects_incorrect_resource_or_action_declaration():
    route_key = ("GET", "/accounts")
    incorrect_policies = dict(ROUTE_POLICIES)
    incorrect_policies[route_key] = RoutePolicy(
        Access.OWNER,
        ResourceType.ACCOUNT,
        Action.READ,
    )

    route_keys = {
        key
        for route in _all_routes()
        for key in _route_keys(route)
    }
    with pytest.raises(AssertionError, match="GET', '/accounts"):
        _assert_route_contract(incorrect_policies, route_keys)


def test_router_level_guard_is_declared_on_owner_scoped_routers():
    for router in (
        accounts.router,
        transactions.router,
        transfers.router,
        causal_events.router,
        reconciliations.router,
        projections.router,
        providers.router,
    ):
        assert any(
            d.dependency in (auth.get_current_user, auth.authorize_owner_route, auth.authorize_owner_principal)
            for d in router.dependencies
        ), router.prefix


def test_policy_is_owner_only_and_denies_unknown_owner():
    alice = authz.Subject(user_id=1)
    res = authz.ResourceType.ACCOUNT
    pol = authz.RoutePolicy(Access.OWNER, res, authz.Action.READ)
    act = authz.Action.READ
    assert authz.evaluate(alice, pol, authz.Resource(res, 1), act).allowed
    assert not authz.evaluate(alice, pol, authz.Resource(res, 2), act).allowed
    assert not authz.evaluate(alice, pol, authz.Resource(res, None), act).allowed


# -------------------------------------------------------------------- helpers


def _user(prefix):
    email = f"{prefix}-{uuid.uuid4().hex}@example.com"
    r = client.post("/auth/register", json={"email": email, "password": "secret123"})
    assert r.status_code == 200, r.text
    t = client.post("/auth/login", json={"email": email, "password": "secret123"})
    return {"Authorization": f"Bearer {t.json()['access_token']}"}


def _idem(h):
    return {**h, "Idempotency-Key": uuid.uuid4().hex}


def _account(h, name=None, account_type="BANK"):
    r = client.post(
        "/accounts",
        json={"name": name or f"a-{uuid.uuid4().hex[:6]}", "account_type": account_type},
        headers=h,
    )
    assert r.status_code == 200, r.text
    return r.json()


def _txn(h, amount="100.00", account_id=None, category="food", type_="income"):
    body = {"amount": amount, "description": "d", "category": category, "type": type_}
    if account_id:
        body["account_id"] = account_id
    r = client.post("/transactions", json=body, headers=_idem(h))
    assert r.status_code in (200, 201), r.text
    return r.json()


def _provider_chain(h):
    conn = client.post(
        "/provider-connections",
        json={"provider_name": "Bank", "external_account_id": uuid.uuid4().hex},
        headers=_idem(h),
    ).json()
    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    ing = client.post(
        f"/provider-connections/{conn['id']}/external-transactions",
        json={
            "external_transaction_id": uuid.uuid4().hex,
            "observed_at": now.isoformat(),
            "raw_payload": {"amount": "5"},
        },
        headers=_idem(h),
    ).json()
    norm = client.post(
        f"/provider-connections/{conn['id']}/external-transactions/"
        f"{ing['external_transaction_record_id']}/normalizations",
        json={
            "evidence_id": ing["evidence_id"],
            "normalizer_version": "v1",
            "amount": "5.00",
            "currency": "VND",
            "direction": "INFLOW",
            "normalized_status": "POSTED",
            "occurred_at": now.isoformat(),
        },
        headers=_idem(h),
    ).json()
    return conn["id"], ing["external_transaction_record_id"], ing["evidence_id"], norm


def snapshot():
    """Order-independent content digest of every table."""
    out = {}
    with engine.connect() as conn:
        for table in Base.metadata.sorted_tables:
            q = engine.dialect.identifier_preparer.quote(table.name)
            out[table.name] = conn.execute(
                text(
                    f"SELECT count(*), md5(coalesce(string_agg(t::text, '|' "
                    f"ORDER BY t::text), '')) FROM {q} t"
                )
            ).one()
    return out


@pytest.fixture
def world():
    alice, mallory = _user("alice"), _user("mallory")
    acct_a = _account(alice)
    acct_a2 = _account(alice)
    cash = _account(alice, account_type="CASH")
    txn_a = _txn(alice, account_id=acct_a["id"])
    _txn(alice, "50.00", account_id=acct_a2["id"], type_="expense")
    transfer = client.post(
        "/transfers",
        json={"amount": "10.00", "from_account_id": acct_a["id"], "to_account_id": acct_a2["id"]},
        headers=_idem(alice),
    )
    assert transfer.status_code in (200, 201), transfer.text
    recon = client.post(
        "/reconciliations",
        json={"account_id": cash["id"], "observed_balance": "1.00"},
        headers=_idem(alice),
    )
    assert recon.status_code in (200, 201), recon.text
    conn_id, rec_id, ev_id, norm = _provider_chain(alice)
    own_conn, own_rec, own_ev, _ = _provider_chain(mallory)
    return {
        "alice": alice,
        "mallory": mallory,
        "acct": acct_a,
        "acct2": acct_a2,
        "cash": cash,
        "txn": txn_a,
        "recon": recon.json(),
        "conn": conn_id,
        "rec": rec_id,
        "ev": ev_id,
        "norm": norm,
        "m_conn": own_conn,
        "m_rec": own_rec,
        "m_ev": own_ev,
    }


# --------------------------------------------------------- cross-user denials


def _denied_requests(w):
    a, t = w["acct"]["id"], w["txn"]["id"]
    c, r, e = w["conn"], w["rec"], w["ev"]
    rid = w["recon"]["id"]
    etx = f"/provider-connections/{c}/external-transactions/{r}"
    v = {"X-Expected-Version": "1"}
    return [
        ("GET", f"/accounts/{a}", None, {}),
        ("GET", f"/transactions/{t}", None, {}),
        ("GET", f"/transactions/{t}/history", None, {}),
        ("PUT", f"/transactions/{t}", {"amount": "1.00", "description": "x", "category": "x", "type": "income"}, v),
        ("DELETE", f"/transactions/{t}", None, v),
        ("POST", "/transactions", {"amount": "1.00", "description": "x", "category": "x", "type": "income", "account_id": a}, {}),
        ("POST", "/transfers", {"amount": "1.00", "from_account_id": a, "to_account_id": w["acct2"]["id"]}, {}),
        ("POST", "/refunds", {"original_event_id": w["txn"]["canonical_event_id"], "amount": "1.00"}, {}),
        ("POST", "/reversals", {"original_event_id": w["txn"]["canonical_event_id"]}, {}),
        ("GET", f"/reconciliations/{rid}", None, {}),
        ("POST", "/reconciliations", {"account_id": w["cash"]["id"], "observed_balance": "1.00"}, {}),
        ("POST", f"/reconciliations/{rid}/resolve", {}, v),
        ("POST", f"/reconciliations/{rid}/adjustments", {"confirm": True, "reason": "attack"}, v),
        ("GET", f"/provider-connections/{c}", None, {}),
        ("GET", f"/provider-connections/{c}/sync-checkpoint", None, {}),
        ("GET", f"/provider-connections/{c}/sync-pages", None, {}),
        ("GET", f"/provider-connections/{c}/external-transactions", None, {}),
        ("POST", f"/provider-connections/{c}/external-transactions", {"external_transaction_id": "x", "raw_payload": {}}, {}),
        ("GET", f"{etx}/evidence", None, {}),
        ("GET", f"{etx}/interpretation", None, {}),
        ("GET", f"{etx}/lifecycle", None, {}),
        ("POST", f"{etx}/normalizations", {"evidence_id": e, "normalizer_version": "z", "amount": "1.00", "currency": "VND", "direction": "INFLOW", "normalized_status": "POSTED", "occurred_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}, {}),
        ("POST", f"{etx}/classification", {"event_type": "INCOME"}, v),
        ("POST", f"{etx}/confirm", {"event_type": "INCOME", "account_id": a}, v),
    ]


def test_cross_user_ids_are_denied_with_404_and_no_side_effects(world):
    mallory = world["mallory"]
    before = snapshot()
    results = []
    for method, path, body, extra in _denied_requests(world):
        r = client.request(method, path, json=body, headers={**_idem(mallory), **extra})
        results.append((method, path, r.status_code))
    # Existing contract: ownership failures are 404 (never 403/200).
    for method, path, code in results:
        # Pre-existing contract: causal commands report an unowned original
        # event as 422, identical to a non-existent one (checked below).
        expected = 422 if path in ("/refunds", "/reversals") else 404
        assert code == expected, (method, path, code)
    ghost = client.post(
        "/refunds",
        json={"original_event_id": 999999, "amount": "1.00"},
        headers=_idem(mallory),
    )
    foreign = client.post(
        "/refunds",
        json={"original_event_id": world["txn"]["canonical_event_id"], "amount": "1.00"},
        headers=_idem(mallory),
    )
    assert (ghost.status_code, ghost.json()) == (foreign.status_code, foreign.json())
    assert snapshot() == before, "denied writes must not change any table"


def test_nested_resource_with_mismatched_parent_is_denied(world):
    """Mallory's own connection combined with alice's transaction-record id."""
    mallory = world["mallory"]
    base = f"/provider-connections/{world['m_conn']}/external-transactions/{world['rec']}"
    before = snapshot()
    for suffix in ("/evidence", "/interpretation", "/lifecycle"):
        assert client.get(base + suffix, headers=mallory).status_code == 404, suffix
    r = client.post(
        base + "/normalizations",
        json={"evidence_id": world["ev"], "normalizer_version": "q", "amount": "1.00", "currency": "VND", "direction": "INFLOW", "normalized_status": "POSTED", "occurred_at": datetime.datetime.now(datetime.timezone.utc).isoformat()},
        headers=_idem(mallory),
    )
    assert r.status_code == 404
    # Own connection + own record but alice's account on confirm.
    own = f"/provider-connections/{world['m_conn']}/external-transactions/{world['m_rec']}"
    r = client.post(
        own + "/confirm",
        json={"event_type": "INCOME", "account_id": world["acct"]["id"]},
        headers={**_idem(mallory), "X-Expected-Version": "1"},
    )
    assert r.status_code in (404, 409, 422) and r.status_code != 200
    assert snapshot() == before


def test_owner_still_succeeds_on_same_resources(world):
    alice = world["alice"]
    assert client.get(f"/accounts/{world['acct']['id']}", headers=alice).status_code == 200
    assert client.get(f"/transactions/{world['txn']['id']}", headers=alice).status_code == 200
    assert client.get(f"/reconciliations/{world['recon']['id']}", headers=alice).status_code == 200
    assert client.get(f"/provider-connections/{world['conn']}", headers=alice).status_code == 200


def test_unauthenticated_and_bad_tokens_keep_401_contract_before_validation():
    for method, path, body in [
        ("GET", "/accounts", None),
        ("POST", "/accounts", {"invalid": True}),  # invalid body must still be 401
        ("GET", "/transactions/1", None),
        ("POST", "/transfers", {}),
        ("GET", "/reconciliations/1", None),
        ("GET", "/projections/status", None),
        ("GET", "/provider-connections", None),
        ("POST", "/refunds", {}),
        ("GET", "/summary", None),
        ("GET", "/auth/me", None),
    ]:
        for headers in ({}, {"Authorization": "Bearer garbage"}):
            r = client.request(method, path, json=body, headers=headers)
            assert r.status_code == 401, (method, path, r.status_code)
            assert r.headers["www-authenticate"] == "Bearer"
            assert r.json()["detail"] == "Invalid authentication credentials"


# ------------------------------------------------- pagination and aggregation


def test_pagination_and_aggregates_are_isolated(world):
    alice, mallory = world["alice"], world["mallory"]
    for i in range(7):
        _txn(alice, f"{i + 1}.00", category="shared")
    for i in range(3):
        _txn(mallory, "1000.00", category="shared")

    seen_ids = set()
    skip = 0
    while True:
        page = client.get(f"/transactions?category=shared&skip={skip}&limit=3", headers=mallory).json()
        assert page["total"] == 3
        seen_ids |= {t["id"] for t in page["data"]}
        skip += 3
        if skip >= page["total"]:
            break
    all_mine = client.get("/transactions?limit=100", headers=mallory).json()
    assert {t["user_id"] for t in all_mine["data"]} == {all_mine["data"][0]["user_id"]}
    assert world["txn"]["id"] not in {t["id"] for t in all_mine["data"]}
    assert seen_ids <= {t["id"] for t in all_mine["data"]}
    # Out-of-range page leaks nothing.
    far = client.get("/transactions?skip=1000&limit=100", headers=mallory).json()
    assert far["data"] == [] and far["total"] == all_mine["total"]

    summary = client.get("/summary", headers=mallory).json()
    assert str(summary["total_income"]).startswith("3000")
    assert str(summary["total_expense"]) in ("0", "0.0", "0.00")

    assert client.post("/projections/rebuild", headers=mallory).status_code == 200
    proj = client.get("/projections/summary", headers=mallory).json()
    assert "3000" in str(proj)
    accts = client.get("/projections/accounts", headers=mallory).json()
    owned = {a["id"] for a in client.get("/accounts", headers=mallory).json()}
    text_blob = str(accts)
    assert str(world["acct"]["id"]) not in {str(i) for i in owned}
    assert "10000" not in text_blob
    listed = client.get("/accounts", headers=mallory).json()
    assert world["acct"]["id"] not in {a["id"] for a in listed}
    assert client.get("/provider-connections", headers=mallory).json() != []
    assert world["conn"] not in {c["id"] for c in client.get("/provider-connections", headers=mallory).json()}


# ------------------------------------------- query counts and session counts


def _measure(path, headers):
    statements, sessions = [], set()

    def on_exec(conn, cursor, statement, *a):
        statements.append(statement)

    def on_begin(session, transaction, connection):
        sessions.add(id(session))

    event.listen(engine, "before_cursor_execute", on_exec)
    event.listen(SessionLocal, "after_begin", on_begin)
    try:
        r = client.get(path, headers=headers)
    finally:
        event.remove(engine, "before_cursor_execute", on_exec)
        event.remove(SessionLocal, "after_begin", on_begin)
    assert r.status_code == 200, r.text
    return len(statements), len(sessions)


@pytest.mark.parametrize("path", ["/accounts", "/transactions?limit=100", "/provider-connections"])
def test_list_endpoints_have_constant_query_count_and_no_extra_sessions(path):
    h = _user("scale")
    _account(h)
    _txn(h)
    _provider_chain(h)
    small = _measure(path, h)
    for _ in range(8):
        _account(h)
        _txn(h)
        _provider_chain(h)
    large = _measure(path, h)
    assert large[0] == small[0], f"N+1 on {path}: {small} -> {large}"
    # Pre-existing baseline: one session for authentication + one for the handler.
    assert large[1] <= 2, large


# ------------------------------------------------ Phase 2: runtime enforcement

from fastapi import Depends  # noqa: E402
from app.authorization import (  # noqa: E402
    Action,
    ResourceType,
    RoutePolicy,
    Subject,
)
from app.models import FinancialAccount  # noqa: E402


def test_evaluator_decisions_come_from_the_routes_own_policy():
    alice = authz.Subject(1)
    get = lambda k: ROUTE_POLICIES.get(k)  # noqa: E731
    assert authz.evaluate_route(get(("GET", "/accounts")), alice).allowed
    assert not authz.evaluate_route(get(("GET", "/accounts")), None).allowed
    assert authz.evaluate_route(get(("GET", "/")), None).allowed  # public
    assert authz.evaluate_route(get(("GET", "/observability/metrics")), None).allowed
    missing = authz.evaluate_route(get(("GET", "/not-declared")), alice)
    assert not missing.allowed and "undefined" in missing.reason
    pol = get(("GET", "/accounts/{account_id}"))
    res = authz.Resource(ResourceType.ACCOUNT, 1)
    assert authz.evaluate(alice, pol, res, Action.READ).allowed
    assert not authz.evaluate(alice, pol, authz.Resource(ResourceType.ACCOUNT, 2), Action.READ).allowed
    # The READ route does not authorize DELETE even for the owner.
    assert not authz.evaluate(alice, pol, res, Action.DELETE).allowed


def test_undeclared_route_is_denied_by_default_at_runtime():
    async def probe():
        return {"ok": True}

    app.add_api_route("/__undeclared_probe", probe, methods=["GET"])
    try:
        r = client.get("/__undeclared_probe")
        assert r.status_code == 403, r.text
    finally:
        app.router.routes.pop()


def test_removing_a_policy_denies_the_live_endpoint(world, monkeypatch):
    alice = world["alice"]
    assert client.get("/accounts", headers=alice).status_code == 200
    monkeypatch.delitem(ROUTE_POLICIES, ("GET", "/accounts"))
    denied = client.get("/accounts", headers=alice)
    assert denied.status_code == 403
    assert client.get("/accounts", headers={}).status_code == 403
    monkeypatch.undo()
    assert client.get("/accounts", headers=alice).status_code == 200


def test_changing_object_policy_changes_owner_decision_but_not_cross_user_404(world, monkeypatch):
    alice, mallory = world["alice"], world["mallory"]
    path = f"/accounts/{world['acct']['id']}"
    assert client.get(path, headers=alice).status_code == 200
    # Re-declare the route as a different resource/action: the route-level
    # check still passes, but no policy grants ACCOUNT/READ to the handler.
    monkeypatch.setitem(
        ROUTE_POLICIES,
        ("GET", "/accounts/{account_id}"),
        RoutePolicy(Access.OWNER, ResourceType.TRANSACTION, Action.READ),
    )
    assert client.get(path, headers=alice).status_code == 403
    assert client.get(path, headers=mallory).status_code == 404  # contract kept
    assert client.get(path).status_code == 401


def test_public_and_metrics_routes_are_unchanged(monkeypatch):
    assert client.get("/").status_code == 200
    assert client.get("/health/live").status_code == 200
    monkeypatch.delenv("METRICS_ACCESS_TOKEN", raising=False)
    assert client.get("/observability/metrics").status_code == 503
    monkeypatch.setenv("METRICS_ACCESS_TOKEN", "tok-123")
    assert client.get("/observability/metrics", headers={"X-Metrics-Token": "bad"}).status_code == 401
    assert client.get("/observability/metrics", headers={"X-Metrics-Token": "tok-123"}).status_code == 200


def test_valid_jwt_alone_does_not_grant_access_to_other_users_objects(world):
    mallory = world["mallory"]
    r = client.get(f"/transactions/{world['txn']['id']}", headers=mallory)
    assert r.status_code == 404
    r = client.get(f"/provider-connections/{world['conn']}", headers=mallory)
    assert r.status_code == 404


def test_owned_scope_is_applied_in_sql_before_limit_and_count(world):
    mallory_id = client.get("/auth/me", headers=world["mallory"]).json()["id"]
    db = SessionLocal()
    try:
        q = authz.owned(db.query(FinancialAccount), FinancialAccount.user_id, Subject(mallory_id))
        sql = str(q.limit(1).offset(0).statement.compile(engine))
        assert sql.index("user_id") < sql.index("LIMIT")
        rows = q.all()
        assert rows and {a.user_id for a in rows} == {mallory_id}
        assert q.count() == len(rows)
    finally:
        db.close()


@pytest.mark.parametrize(
    "path_key",
    ["/accounts", "/accounts/{a}", "/transactions?limit=100", "/transactions/{t}",
     "/provider-connections", "/provider-connections/{c}", "/projections/status", "/summary"],
)
def test_pool_checkouts_per_request_do_not_grow(world, path_key):
    path = path_key.format(a=world["acct"]["id"], t=world["txn"]["id"], c=world["conn"])
    checkouts = []
    listener = lambda *a: checkouts.append(1)  # noqa: E731
    event.listen(engine, "checkout", listener)
    try:
        r = client.get(path, headers=world["alice"])
    finally:
        event.remove(engine, "checkout", listener)
    assert r.status_code == 200, r.text
    assert len(checkouts) <= 2, (path, len(checkouts))


# ------------------------------- Phase 2b: route-bound decisions, unpoliced mounts

from fastapi import FastAPI  # noqa: E402


def test_tautological_global_grant_lookup_is_gone():
    assert not hasattr(authz, "_granted")
    assert not hasattr(authz, "is_allowed")


def test_object_decision_is_bound_to_the_supplied_route_policy_only():
    alice = Subject(1)
    own = Resource_ = authz.Resource(ResourceType.ACCOUNT, 1)
    read = RoutePolicy(Access.OWNER, ResourceType.ACCOUNT, Action.READ)
    assert authz.evaluate(alice, read, own, Action.READ).allowed
    # Same resource, different action than this route declares.
    assert not authz.evaluate(alice, read, own, Action.DELETE).allowed
    # Policy for a different resource.
    other = RoutePolicy(Access.OWNER, ResourceType.TRANSACTION, Action.READ)
    assert not authz.evaluate(alice, other, own, Action.READ).allowed
    # Non-owner routes never grant object access.
    assert not authz.evaluate(alice, RoutePolicy(Access.PUBLIC), own, Action.READ).allowed
    assert not authz.evaluate(alice, None, own, Action.READ).allowed
    # Ownership still required.
    assert not authz.evaluate(alice, read, authz.Resource(ResourceType.ACCOUNT, 2), Action.READ).allowed
    assert not authz.evaluate(None, read, own, Action.READ).allowed


def test_route_decision_uses_the_given_policy_not_a_table_search():
    alice = Subject(1)
    assert authz.evaluate_route(None, alice).reason == "undefined route policy"
    assert authz.evaluate_route(RoutePolicy(Access.PUBLIC), None).allowed
    assert authz.evaluate_route(RoutePolicy(Access.METRICS_TOKEN), None).allowed
    full = RoutePolicy(Access.OWNER, ResourceType.ACCOUNT, Action.LIST)
    assert authz.evaluate_route(full, alice).allowed
    assert not authz.evaluate_route(full, None).allowed
    # An owner policy without resource/action is malformed and denied.
    assert not authz.evaluate_route(RoutePolicy(Access.OWNER), alice).allowed


def test_all_owner_policies_are_fully_specified():
    for key, p in ROUTE_POLICIES.items():
        if p.access is Access.OWNER:
            assert p.resource is not None and p.action is not None, key
        else:
            assert p.resource is None and p.action is None, key


def test_handler_cannot_borrow_a_grant_from_another_route(world, monkeypatch):
    alice = world["alice"]
    path = f"/accounts/{world['acct']['id']}"
    assert client.get(path, headers=alice).status_code == 200
    # This route now declares ACCOUNT/LIST while the handler implements
    # ACCOUNT/READ; a *different* route declares ACCOUNT/READ.
    monkeypatch.setitem(
        ROUTE_POLICIES,
        ("GET", "/accounts/{account_id}"),
        RoutePolicy(Access.OWNER, ResourceType.ACCOUNT, Action.LIST),
    )
    monkeypatch.setitem(
        ROUTE_POLICIES,
        ("GET", "/__elsewhere"),
        RoutePolicy(Access.OWNER, ResourceType.ACCOUNT, Action.READ),
    )
    assert client.get(path, headers=alice).status_code == 403
    assert client.get(path, headers=world["mallory"]).status_code == 404


def test_owner_guard_on_a_route_not_declared_owner_is_denied(world, monkeypatch):
    monkeypatch.setitem(ROUTE_POLICIES, ("GET", "/accounts"), RoutePolicy(Access.PUBLIC))
    assert client.get("/accounts", headers=world["alice"]).status_code == 403


class _Unpoliced:
    """Register a route kind the HTTP policy guard cannot see, then clean up."""

    def __enter__(self):
        self.before = list(app.router.routes)
        return self

    def __exit__(self, *exc):
        app.router.routes[:] = self.before


def test_startup_inventory_accepts_current_app():
    authz.assert_only_policed_routes(app)


def test_mounted_subapp_is_prohibited():
    with _Unpoliced():
        sub = FastAPI()

        @sub.get("/leak")
        def leak():
            return {"leak": 1}

        app.mount("/sub", sub)
        with pytest.raises(RuntimeError, match="unpoliced"):
            authz.assert_only_policed_routes(app)


def test_websocket_route_is_prohibited():
    with _Unpoliced():

        @app.websocket("/ws-leak")
        async def ws(websocket):
            await websocket.accept()

        with pytest.raises(RuntimeError, match="unpoliced"):
            authz.assert_only_policed_routes(app)


def test_websocket_inside_included_router_is_prohibited():
    with _Unpoliced():
        from fastapi import APIRouter

        r = APIRouter(prefix="/nested")

        @r.websocket("/ws")
        async def ws(websocket):
            await websocket.accept()

        app.include_router(r)
        with pytest.raises(RuntimeError, match="unpoliced"):
            authz.assert_only_policed_routes(app)


def test_process_refuses_to_start_with_unpoliced_mount():
    with _Unpoliced():
        app.mount("/sub2", FastAPI())
        with pytest.raises(RuntimeError, match="unpoliced"):
            with TestClient(app):
                pass
