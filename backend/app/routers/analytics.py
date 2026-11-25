"""Read-only, owner-scoped, freshness-gated analysis context for PFP V2.

This deliberately exposes an all-time summary only. Monthly attribution and
refund/reversal semantics require a separate reviewed financial specification.
"""

import asyncio
import contextlib
import json
import os
from datetime import datetime, timezone
from typing import NamedTuple

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from app.analysis_context import build_analysis_context
from app.database import SessionLocal
from app.routers.auth import Principal, authorize_owner_principal, get_principal
from app.routers.projections import get_db, _fresh_state_or_http_error

MAX_GO_RESPONSE_BYTES = 64 * 1024


class GoResponse(NamedTuple):
    status_code: int
    headers: dict[str, str]
    content: bytes


class AnalysisProtocolError(ValueError):
    pass

router = APIRouter(
    prefix="/analytics",
    tags=["analytics"],
    dependencies=[Depends(authorize_owner_principal)],
)


@router.get("/context")
def get_analysis_context(
    db: Session = Depends(get_db),
    current_user: Principal = Depends(get_principal),
):
    """Snapshot of *all-time* canonical projection; never executes a rebuild.

    Rejects missing/stale projections with the same 409 semantics as the
    existing /projections/summary endpoint. Does not expose account IDs or
    sensitive transaction descriptions to the LLM.
    """
    state = _fresh_state_or_http_error(db, current_user.id)
    return build_analysis_context(user_id=current_user.id, state=state)


def get_ai_http_client(request: Request) -> httpx.AsyncClient:
    return request.app.state.ai_http_client


def _build_fresh_context(user_id: int) -> dict:
    # Keep the synchronous ORM session and all its queries within one worker
    # thread. No ORM objects or live sessions cross into the async route.
    with SessionLocal() as db:
        state = _fresh_state_or_http_error(db, user_id)
        return build_analysis_context(user_id=user_id, state=state)


def _fresh_projection_identity(user_id: int) -> tuple[int, str]:
    with SessionLocal() as db:
        state = _fresh_state_or_http_error(db, user_id)
        return int(state.generation), str(state.canonical_fingerprint)


def _validated_result(payload: object) -> dict:
    if not isinstance(payload, dict) or set(payload) != {"summary", "findings", "limitations"}:
        raise ValueError("invalid result object")
    if not isinstance(payload["summary"], str) or not isinstance(payload["findings"], list):
        raise ValueError("invalid result fields")
    if not isinstance(payload["limitations"], list) or any(
        not isinstance(item, str) for item in payload["limitations"]
    ):
        raise ValueError("invalid limitations")
    for finding in payload["findings"]:
        if (
            not isinstance(finding, dict)
            or set(finding) != {"metric", "description", "evidence"}
            or not isinstance(finding["metric"], str)
            or not isinstance(finding["description"], str)
            or not isinstance(finding["evidence"], list)
            or any(not isinstance(item, str) for item in finding["evidence"])
        ):
            raise ValueError("invalid finding")
    return payload


async def _call_go_with_disconnect(
    request: Request,
    client: httpx.AsyncClient,
    url: str,
    token: str,
    context: dict,
) -> GoResponse:
    call = asyncio.create_task(
        _post_go_analysis(client, url, token, context)
    )
    try:
        async with asyncio.timeout(10.0):
            while not call.done():
                await asyncio.wait({call}, timeout=0.05)
                if not call.done() and await request.is_disconnected():
                    raise HTTPException(status_code=499, detail="client_disconnected")
            return await call
    except BaseException:
        if not call.done():
            call.cancel()
        with contextlib.suppress(BaseException):
            await call
        raise


async def _post_go_analysis(
    client: httpx.AsyncClient, url: str, token: str, context: dict
) -> GoResponse:
    async with client.stream(
        "POST",
        url,
        headers={"X-Internal-Token": token},
        json={"context": context},
    ) as response:
        content = bytearray()
        async for chunk in response.aiter_bytes():
            if len(content) + len(chunk) > MAX_GO_RESPONSE_BYTES:
                raise AnalysisProtocolError("response exceeded size limit")
            content.extend(chunk)
        return GoResponse(response.status_code, dict(response.headers), bytes(content))


@router.post("/insights")
async def get_financial_insights(
    request: Request,
    current_user: Principal = Depends(get_principal),
    client: httpx.AsyncClient = Depends(get_ai_http_client),
):
    """Return analysis of the authenticated owner's current projection snapshot.

    This endpoint intentionally accepts no financial input from the caller.
    """
    async for chunk in request.stream():
        if chunk:
            raise HTTPException(status_code=400, detail="request_body_not_allowed")

    token = os.getenv("PFP_AI_INTERNAL_TOKEN", "")
    if len(token) < 32:
        raise HTTPException(status_code=503, detail="analysis_service_unavailable")
    base_url = os.getenv("PFP_AI_SERVICE_URL", "http://ai-service:8091").rstrip("/")

    try:
        context = await run_in_threadpool(_build_fresh_context, current_user.id)
    except HTTPException:
        raise

    try:
        response = await _call_go_with_disconnect(
            request,
            client,
            f"{base_url}/internal/analyze",
            token,
            context,
        )
    except HTTPException:
        raise
    except AnalysisProtocolError as exc:
        raise HTTPException(status_code=502, detail="analysis_invalid_response") from exc
    except (httpx.TimeoutException, TimeoutError) as exc:
        raise HTTPException(status_code=504, detail="analysis_timeout") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="analysis_service_unavailable") from exc

    if response.status_code == 503:
        headers = {}
        retry_after = response.headers.get("retry-after")
        if retry_after and retry_after.isdigit():
            headers["Retry-After"] = retry_after
        raise HTTPException(status_code=503, detail="analysis_overloaded", headers=headers)
    if response.status_code == 504:
        raise HTTPException(status_code=504, detail="analysis_timeout")
    if response.status_code == 409:
        raise HTTPException(status_code=409, detail="analysis_context_stale")
    if response.status_code != 200:
        raise HTTPException(status_code=502, detail="analysis_service_failure")
    try:
        analysis = _validated_result(json.loads(response.content))
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=502, detail="analysis_invalid_response") from exc

    try:
        generation, fingerprint = await run_in_threadpool(
            _fresh_projection_identity, current_user.id
        )
    except HTTPException as exc:
        if exc.status_code == 409:
            raise HTTPException(status_code=409, detail="analysis_snapshot_changed") from exc
        raise
    if (
        generation != context["generation"]
        or fingerprint != context["freshness"]["canonical_fingerprint"]
    ):
        raise HTTPException(status_code=409, detail="analysis_snapshot_changed")

    verified_at = datetime.now(timezone.utc).isoformat()
    return {
        "snapshot_id": context["snapshot_id"],
        "schema_version": context["schema_version"],
        "generated_at": context["generated_at"],
        "freshness": {
            "status": "FRESH",
            "generation": generation,
            "canonical_fingerprint": fingerprint,
            "checked_at": context["freshness"]["checked_at"],
            "verified_at": verified_at,
        },
        "analysis": analysis,
    }
