from app.gateway_security import verify_gateway
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.authorization import ROUTE_POLICIES, Subject, evaluate_route
from app.routers.auth import (
    Principal,
    authorize_owner_principal,
    get_principal,
)

router = APIRouter(prefix="/internal/gateway", tags=["gateway-authz"])


class AuthorizationRequest(BaseModel):
    method: str
    path: str


def _account_route_policy_path(method: str, path: str) -> str | None:
    if path == "/accounts":
        return path if method in {"GET", "POST"} else None
    if path == "/accounts/shared-with-me":
        return path if method == "GET" else None

    parts = path.strip("/").split("/")
    if len(parts) not in {2, 3, 4} or parts[0] != "accounts":
        return None
    if not parts[1].isdecimal():
        return None
    if len(parts) == 2 and method == "GET":
        return "/accounts/{account_id}"
    if len(parts) == 3 and parts[2] == "shares" and method in {"GET", "POST"}:
        return f"/accounts/{{account_id}}/shares"
    if (
        len(parts) == 4
        and parts[2] == "shares"
        and parts[3].isdecimal()
        and method == "DELETE"
    ):
        return "/accounts/{account_id}/shares/{user_id}"
    return None


@router.post(
    "/authorize",
    dependencies=[Depends(verify_gateway), Depends(authorize_owner_principal)],
)
def authorize(
    payload: AuthorizationRequest,
    principal: Principal = Depends(get_principal),
):
    method = payload.method.upper()
    path = payload.path

    policy_path = _account_route_policy_path(method, path)
    if policy_path is None:
        return {"allow": False}

    policy = ROUTE_POLICIES.get((method, policy_path))
    decision = evaluate_route(policy, Subject(principal.id))

    # The root account collection remains read-only at the gateway pilot.
    # Share mutations are routed to the backend, which enforces account ownership.
    allowed = decision.allowed and not (path == "/accounts" and method == "POST")

    return {
        "allow": allowed,
        "resource": "account",
        "action": policy.action.value if policy is not None else None,
    }
