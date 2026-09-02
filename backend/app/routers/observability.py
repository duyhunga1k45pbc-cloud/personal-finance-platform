from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.deployment import deployment_readiness, release_metadata
from app.models import User
from app.observability import (
    check_database_readiness,
    durable_operational_snapshot,
    runtime_metrics,
)
from app.routers.auth import get_current_user

router = APIRouter(tags=["observability"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


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
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Operational details are authenticated in V1. A production deployment may
    # expose this through a separate internal network/service identity instead.
    return {
        "runtime": runtime_metrics.snapshot(),
        "durable": durable_operational_snapshot(db),
    }
