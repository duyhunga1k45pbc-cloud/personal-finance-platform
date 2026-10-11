from __future__ import annotations

import os
import secrets

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import APIKeyHeader
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.deployment import deployment_readiness, release_metadata
from app.observability import (
    check_database_readiness,
    durable_operational_snapshot,
    runtime_metrics,
)

router = APIRouter(tags=["observability"])
metrics_token_header = APIKeyHeader(
    name="X-Metrics-Token",
    scheme_name="MetricsToken",
    auto_error=False,
)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def authorize_metrics(
    metrics_token: str | None = Depends(metrics_token_header),
) -> None:
    configured_token = os.getenv("METRICS_ACCESS_TOKEN")
    if not configured_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Metrics access is not configured",
        )
    if metrics_token is None or not secrets.compare_digest(
        metrics_token.encode("utf-8"),
        configured_token.encode("utf-8"),
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid metrics credentials",
        )


@router.get("/health/live")
def live():
    # Liveness answers only one question: is the process alive?
    # Keep this contract minimal and stable; release metadata belongs on readiness.
    return {"status": "ok"}


@router.get("/health/ready")
def ready():
    # Keep the TASK 16 database-readiness seam so failure injection/tests can
    # exercise the public endpoint without bypassing TASK 18 deployment checks.
    database = check_database_readiness()
    if not database["ready"]:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=database,
        )

    result = deployment_readiness(database_readiness=database)
    if not result["ready"]:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=result,
        )
    return {"status": "ready", **result}


@router.get("/observability/metrics")
def metrics(
    _: None = Depends(authorize_metrics),
    db: Session = Depends(get_db),
):
    return {
        "runtime": runtime_metrics.snapshot(),
        "durable": durable_operational_snapshot(db),
    }
