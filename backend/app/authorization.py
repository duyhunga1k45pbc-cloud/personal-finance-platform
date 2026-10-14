"""Minimal ownership-based authorization policy model.

Authentication stays in ``app.routers.auth.get_current_user``. This module only
declares which resources exist, who may act on them (ownership) and how every
HTTP route is protected. Ownership is still enforced in SQL by the existing
user-scoped queries; ``ownership_condition`` is the shared way to express it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from fastapi import HTTPException, Request
from sqlalchemy.orm.attributes import InstrumentedAttribute


class Action(str, Enum):
    READ = "read"
    LIST = "list"
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    EXECUTE = "execute"


class ResourceType(str, Enum):
    ACCOUNT = "account"
    TRANSACTION = "transaction"
    TRANSFER = "transfer"
    CAUSAL_EVENT = "causal_event"
    RECONCILIATION = "reconciliation"
    PROJECTION = "projection"
    PROVIDER_CONNECTION = "provider_connection"
    EXTERNAL_TRANSACTION = "external_transaction"
    USER = "user"
    SUMMARY = "summary"


class Access(str, Enum):
    PUBLIC = "public"
    OWNER = "owner"  # authenticated; data scoped to the subject's own rows
    METRICS_TOKEN = "metrics_token"


@dataclass(frozen=True)
class Subject:
    user_id: int


@dataclass(frozen=True)
class Resource:
    type: ResourceType
    owner_id: int | None


@dataclass(frozen=True)
class RoutePolicy:
    access: Access
    resource: ResourceType | None = None
    action: Action | None = None


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str = ""


@dataclass(frozen=True)
class Authorized:
    """Outcome of the router-level guard: the subject and the *current route's* policy."""

    subject: Subject
    policy: RoutePolicy


def evaluate(
    subject: Subject | None,
    policy: RoutePolicy | None,
    resource: Resource,
    action: Action,
) -> Decision:
    """Object-level decision bound to one route's declared policy.

    The route must be owner-scoped and declare exactly this resource type and
    action, and the subject must own the resource. No other route's policy is
    consulted.
    """
    if subject is None:
        return Decision(False, "unauthenticated")
    if policy is None or policy.access is not Access.OWNER:
        return Decision(False, "route is not owner-scoped")
    if policy.resource is not resource.type or policy.action is not action:
        return Decision(False, "route policy does not cover this resource/action")
    if resource.owner_id is None or resource.owner_id != subject.user_id:
        return Decision(False, "not owner")
    return Decision(True)


def evaluate_route(policy: RoutePolicy | None, subject: Subject | None) -> Decision:
    """Route-level decision from the route's own declared policy.

    Authentication is not authorization: an owner route needs an authenticated
    subject *and* a fully specified resource/action. Object ownership is then
    enforced by scoped queries (``owned``) and ``authorize_object``. Metrics
    credentials are verified by the ``authorize_metrics`` dependency.
    """
    if policy is None:
        return Decision(False, "undefined route policy")
    if policy.access is Access.OWNER:
        if subject is None:
            return Decision(False, "unauthenticated")
        if policy.resource is None or policy.action is None:
            return Decision(False, "malformed owner policy")
    return Decision(True)


def route_path(request: Request) -> str:
    route = request.scope.get("route")
    return getattr(route, "path", None) or request.url.path


def policy_for(request: Request) -> RoutePolicy | None:
    return ROUTE_POLICIES.get((request.method.upper(), route_path(request)))


def enforce_declared_policy(request: Request) -> None:
    """App-wide dependency: deny any HTTP route without a declared policy."""
    if policy_for(request) is None:
        raise HTTPException(status_code=403, detail="Forbidden")


def authorize_object(
    authorized: Authorized,
    resource_type: ResourceType,
    action: Action,
    owner_id: int,
) -> None:
    """Handler-side check after an owned lookup; 403 unless this route's own policy covers it."""
    decision = evaluate(
        authorized.subject,
        authorized.policy,
        Resource(resource_type, owner_id),
        action,
    )
    if not decision.allowed:
        raise HTTPException(status_code=403, detail="Forbidden")


_FRAMEWORK_PATHS = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}


def assert_only_policed_routes(application) -> None:
    """Refuse route kinds the HTTP policy guard cannot see.

    ``enforce_declared_policy`` is a dependency of HTTP ``APIRoute`` handlers.
    Mounted sub-apps, WebSocket routes and raw Starlette routes bypass it, so
    they must be reviewed and given their own enforcement before registration.
    """
    from fastapi.routing import APIRoute
    from starlette.routing import Route

    offenders: list[str] = []

    def walk(routes):
        for route in routes:
            nested = getattr(route, "original_router", None)
            if nested is not None:
                walk(nested.routes)
            elif isinstance(route, APIRoute):
                continue
            elif type(route) is Route and route.path in _FRAMEWORK_PATHS:
                continue
            else:
                offenders.append(f"{type(route).__name__}:{getattr(route, 'path', '?')}")

    walk(application.routes)
    if offenders:
        raise RuntimeError(f"unpoliced routes registered: {sorted(offenders)}")


def ownership_condition(owner_column: InstrumentedAttribute, subject: Subject):
    return owner_column == subject.user_id


def owned(query, owner_column: InstrumentedAttribute, subject: Subject):
    """Apply the ownership predicate in SQL, before any count/offset/limit/aggregate."""
    return query.filter(ownership_condition(owner_column, subject))


def _owner(resource: ResourceType, action: Action) -> RoutePolicy:
    return RoutePolicy(Access.OWNER, resource, action)


_PUBLIC = RoutePolicy(Access.PUBLIC)
_P = ResourceType.PROVIDER_CONNECTION
_X = ResourceType.EXTERNAL_TRANSACTION
_ETX = "/provider-connections/{connection_id}/external-transactions"

# (METHOD, path) -> policy. Every HTTP route must be declared here;
# tests/test_authorization_policy.py fails on undeclared or unguarded routes.
ROUTE_POLICIES: dict[tuple[str, str], RoutePolicy] = {
    ("GET", "/"): _PUBLIC,
    ("GET", "/demo"): _PUBLIC,
    ("GET", "/summary"): _owner(ResourceType.SUMMARY, Action.READ),
    ("POST", "/auth/register"): _PUBLIC,
    ("POST", "/auth/login"): _PUBLIC,
    ("GET", "/auth/me"): _owner(ResourceType.USER, Action.READ),
    ("POST", "/internal/gateway/authorize"): _owner(ResourceType.USER, Action.EXECUTE),
    ("GET", "/health/live"): _PUBLIC,
    ("GET", "/health/ready"): _PUBLIC,
    ("GET", "/observability/metrics"): RoutePolicy(Access.METRICS_TOKEN),
    ("GET", "/accounts"): _owner(ResourceType.ACCOUNT, Action.LIST),
    ("POST", "/accounts"): _owner(ResourceType.ACCOUNT, Action.CREATE),
    ("GET", "/accounts/{account_id}"): _owner(ResourceType.ACCOUNT, Action.READ),
    ("GET", "/transactions"): _owner(ResourceType.TRANSACTION, Action.LIST),
    ("GET", "/transactions/{transaction_id}"): _owner(ResourceType.TRANSACTION, Action.READ),
    ("GET", "/transactions/{transaction_id}/history"): _owner(ResourceType.TRANSACTION, Action.READ),
    ("POST", "/transactions"): _owner(ResourceType.TRANSACTION, Action.CREATE),
    ("PUT", "/transactions/{transaction_id}"): _owner(ResourceType.TRANSACTION, Action.UPDATE),
    ("DELETE", "/transactions/{transaction_id}"): _owner(ResourceType.TRANSACTION, Action.DELETE),
    ("POST", "/transfers"): _owner(ResourceType.TRANSFER, Action.CREATE),
    ("POST", "/refunds"): _owner(ResourceType.CAUSAL_EVENT, Action.CREATE),
    ("POST", "/reversals"): _owner(ResourceType.CAUSAL_EVENT, Action.CREATE),
    ("GET", "/reconciliations/{reconciliation_id}"): _owner(ResourceType.RECONCILIATION, Action.READ),
    ("POST", "/reconciliations"): _owner(ResourceType.RECONCILIATION, Action.CREATE),
    ("POST", "/reconciliations/{reconciliation_id}/resolve"): _owner(ResourceType.RECONCILIATION, Action.UPDATE),
    ("POST", "/reconciliations/{reconciliation_id}/adjustments"): _owner(ResourceType.RECONCILIATION, Action.UPDATE),
    ("POST", "/projections/rebuild"): _owner(ResourceType.PROJECTION, Action.EXECUTE),
    ("GET", "/projections/status"): _owner(ResourceType.PROJECTION, Action.READ),
    ("GET", "/projections/summary"): _owner(ResourceType.PROJECTION, Action.READ),
    ("GET", "/projections/accounts"): _owner(ResourceType.PROJECTION, Action.LIST),
    ("POST", "/provider-connections"): _owner(_P, Action.CREATE),
    ("GET", "/provider-connections"): _owner(_P, Action.LIST),
    ("GET", "/provider-connections/{connection_id}"): _owner(_P, Action.READ),
    ("GET", "/provider-connections/{connection_id}/sync-checkpoint"): _owner(_P, Action.READ),
    ("GET", "/provider-connections/{connection_id}/sync-pages"): _owner(_P, Action.LIST),
    ("POST", _ETX): _owner(_X, Action.CREATE),
    ("GET", _ETX): _owner(_X, Action.LIST),
    ("GET", _ETX + "/{transaction_record_id}/evidence"): _owner(_X, Action.READ),
    ("POST", _ETX + "/{transaction_record_id}/normalizations"): _owner(_X, Action.EXECUTE),
    ("GET", _ETX + "/{transaction_record_id}/interpretation"): _owner(_X, Action.READ),
    ("GET", _ETX + "/{transaction_record_id}/lifecycle"): _owner(_X, Action.READ),
    ("POST", _ETX + "/{transaction_record_id}/classification"): _owner(_X, Action.UPDATE),
    ("POST", _ETX + "/{transaction_record_id}/confirm"): _owner(_X, Action.UPDATE),
}
