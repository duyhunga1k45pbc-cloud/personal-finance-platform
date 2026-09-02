import logging
import time

from fastapi import Depends, FastAPI, Request
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app import models
from app.canonical_service import canonical_summary
from app.database import SessionLocal
from app.deployment import assert_startup_ready, mark_process_draining, mark_process_serving
from app.models import User
from app.observability import (
    bind_request_id,
    increment_metric,
    log_event,
    observe_http_request,
    reset_request_id,
    sanitize_request_id,
)
from app.routers import accounts, auth, causal_events, observability, projections, providers, reconciliations, transactions, transfers
from app.routers.auth import get_current_user
from app.security import settings

app = FastAPI(
    title="Personal Finance API",
    description="Backend API quản lý thu chi cá nhân",
    version="1.0.0",
    docs_url=None if settings.is_production else "/docs",
    redoc_url=None if settings.is_production else "/redoc",
    openapi_url=None if settings.is_production else "/openapi.json",
)


@app.on_event("startup")
def deployment_startup_gate():
    assert_startup_ready()
    mark_process_serving()


@app.on_event("shutdown")
def deployment_shutdown_gate():
    mark_process_draining()


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    # Financial responses and bearer-token responses should not be cached by
    # browsers or intermediary proxies unless a future endpoint opts in.
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return response


@app.middleware("http")
async def observe_request(request: Request, call_next):
    request_id = sanitize_request_id(request.headers.get("X-Request-ID"))
    token = bind_request_id(request_id)
    started = time.perf_counter()
    status_code = 500
    route_name = "<unmatched>"
    try:
        response = await call_next(request)
        status_code = response.status_code
        route = request.scope.get("route")
        route_name = getattr(route, "path", "<unmatched>")
        response.headers["X-Request-ID"] = request_id
        return response
    except SQLAlchemyError:
        increment_metric("db_errors_total")
        route = request.scope.get("route")
        route_name = getattr(route, "path", "<unmatched>")
        log_event(
            "http.database_error",
            level=logging.ERROR,
            method=request.method,
            route=route_name,
        )
        raise
    except Exception:
        route = request.scope.get("route")
        route_name = getattr(route, "path", "<unmatched>")
        log_event(
            "http.request_failed",
            level=logging.ERROR,
            method=request.method,
            route=route_name,
        )
        raise
    finally:
        duration_ms = (time.perf_counter() - started) * 1000
        observe_http_request(
            method=request.method,
            route=route_name,
            status_code=status_code,
            duration_ms=duration_ms,
        )
        log_event(
            "http.request_completed",
            method=request.method,
            route=route_name,
            status_code=status_code,
            duration_ms=round(duration_ms, 3),
        )
        reset_request_id(token)

app.include_router(auth.router)
app.include_router(accounts.router)
app.include_router(transactions.router)
app.include_router(transfers.router)
app.include_router(causal_events.router)
app.include_router(reconciliations.router)
app.include_router(observability.router)
app.include_router(projections.router)
app.include_router(providers.router)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@app.get("/")
def home():
    return {"message": "Chào mừng bạn đến với ứng dụng Quản lý Tài chính!"}


@app.get("/summary")
def get_summary(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    summary = canonical_summary(db, current_user.id)
    return {
        "total_income": summary.total_income,
        "total_expense": summary.total_expense,
        "balance": summary.balance,
    }
