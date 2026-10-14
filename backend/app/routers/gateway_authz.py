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

    # Only the pilot resource is supported.
    if path != "/accounts" or method not in {"GET", "POST"}:
        return {"allow": False}

    policy = ROUTE_POLICIES.get((method, path))
    decision = evaluate_route(policy, Subject(principal.id))

    # Temporary gateway policy: read allowed, write denied.
    allowed = decision.allowed and method == "GET"

    return {
        "allow": allowed,
        "resource": "account",
        "action": "list" if method == "GET" else "create",
    }
