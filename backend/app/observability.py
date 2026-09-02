from __future__ import annotations

import contextvars
import datetime
import json
import logging
import re
import sys
import threading
import time
import uuid
from collections import defaultdict
from typing import Any

from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.database import engine
from app.models import (
    FinancialProjectionState,
    ProviderSyncCheckpoint,
    ProviderSyncPage,
    ReconciliationCase,
    User,
)

_REQUEST_ID = contextvars.ContextVar("request_id", default=None)
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_SENSITIVE_KEY_PARTS = (
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
    "api_key",
    "private_key",
    "raw_payload",
)


def utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def sanitize_request_id(candidate: str | None) -> str:
    value = (candidate or "").strip()
    if value and _REQUEST_ID_RE.fullmatch(value):
        return value
    return uuid.uuid4().hex


def bind_request_id(request_id: str):
    return _REQUEST_ID.set(request_id)


def reset_request_id(token) -> None:
    _REQUEST_ID.reset(token)


def current_request_id() -> str | None:
    return _REQUEST_ID.get()


def _sanitize(value: Any, *, key: str | None = None) -> Any:
    if key and any(part in key.lower() for part in _SENSITIVE_KEY_PARTS):
        return "<redacted>"
    if isinstance(value, dict):
        return {
            str(k): _sanitize(v, key=str(k))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value]
    if isinstance(value, datetime.datetime):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": utc_now().isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": getattr(record, "event", record.getMessage()),
        }
        request_id = getattr(record, "request_id", None) or current_request_id()
        if request_id:
            payload["request_id"] = request_id
        fields = getattr(record, "fields", None)
        if fields:
            payload.update(_sanitize(fields))
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))


logger = logging.getLogger("personal_finance.observability")


def configure_observability() -> None:
    if getattr(logger, "_personal_finance_configured", False):
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger._personal_finance_configured = True  # type: ignore[attr-defined]


def log_event(event: str, *, level: int = logging.INFO, **fields: Any) -> None:
    configure_observability()
    logger.log(
        level,
        event,
        extra={
            "event": event,
            "request_id": current_request_id(),
            "fields": fields,
        },
    )


class RuntimeMetrics:
    """Process-local operational counters.

    These metrics intentionally describe what this process has observed since
    startup. Durable financial truth remains in PostgreSQL. Multi-worker
    deployments should scrape every worker or replace this adapter with a
    shared metrics backend; the counters are never used for business decisions.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.started_at = utc_now()
        self._counters: dict[str, int] = defaultdict(int)
        self._http: dict[tuple[str, str, int], dict[str, float]] = {}

    def increment(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._counters[name] += int(amount)

    def observe_http(
        self,
        *,
        method: str,
        route: str,
        status_code: int,
        duration_ms: float,
    ) -> None:
        key = (method.upper(), route, int(status_code))
        with self._lock:
            bucket = self._http.setdefault(
                key,
                {"count": 0, "duration_ms_sum": 0.0, "duration_ms_max": 0.0},
            )
            bucket["count"] += 1
            bucket["duration_ms_sum"] += float(duration_ms)
            bucket["duration_ms_max"] = max(
                bucket["duration_ms_max"],
                float(duration_ms),
            )
            self._counters["http_requests_total"] += 1
            if status_code >= 500:
                self._counters["http_5xx_total"] += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            counters = dict(sorted(self._counters.items()))
            rows = []
            for (method, route, status_code), values in sorted(self._http.items()):
                count = int(values["count"])
                rows.append(
                    {
                        "method": method,
                        "route": route,
                        "status_code": status_code,
                        "count": count,
                        "duration_ms_sum": round(values["duration_ms_sum"], 3),
                        "duration_ms_max": round(values["duration_ms_max"], 3),
                        "duration_ms_avg": round(
                            values["duration_ms_sum"] / count,
                            3,
                        ) if count else 0.0,
                    }
                )
            return {
                "started_at": self.started_at.isoformat(),
                "counters": counters,
                "http": rows,
            }

    def reset_for_tests(self) -> None:
        with self._lock:
            self.started_at = utc_now()
            self._counters.clear()
            self._http.clear()


runtime_metrics = RuntimeMetrics()


def increment_metric(name: str, amount: int = 1) -> None:
    runtime_metrics.increment(name, amount)


def observe_http_request(
    *,
    method: str,
    route: str,
    status_code: int,
    duration_ms: float,
) -> None:
    runtime_metrics.observe_http(
        method=method,
        route=route,
        status_code=status_code,
        duration_ms=duration_ms,
    )


_CRITICAL_TABLES = (
    "users",
    "financial_accounts",
    "financial_events",
    "financial_event_entries",
    "reconciliation_cases",
    "provider_sync_checkpoints",
    "financial_projection_state",
)


def check_database_readiness() -> dict[str, Any]:
    """Check connectivity plus the minimum migrated schema required by V1."""

    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        inspector = inspect(engine)
        missing = [
            table
            for table in _CRITICAL_TABLES
            if not inspector.has_table(table)
        ]
        if missing:
            return {
                "ready": False,
                "database": "ok",
                "schema": "missing",
                "missing_tables": missing,
            }
        return {
            "ready": True,
            "database": "ok",
            "schema": "ok",
        }
    except SQLAlchemyError:
        increment_metric("db_errors_total")
        log_event("database.readiness_failed", level=logging.ERROR)
        return {
            "ready": False,
            "database": "error",
            "schema": "unknown",
        }


def durable_operational_snapshot(db: Session) -> dict[str, Any]:
    """Return low-cardinality durable gauges without exposing financial values."""

    users_total = db.query(User.id).count()
    projection_rows = db.query(FinancialProjectionState.id).count()
    checkpoints = db.query(ProviderSyncCheckpoint).all()

    oldest_age_seconds = None
    if checkpoints:
        now = utc_now()
        ages = []
        for checkpoint in checkpoints:
            updated_at = checkpoint.updated_at
            if updated_at is None:
                continue
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=datetime.timezone.utc)
            ages.append(max((now - updated_at).total_seconds(), 0.0))
        if ages:
            oldest_age_seconds = round(max(ages), 3)

    return {
        "users_total": users_total,
        "open_reconciliation_mismatches": (
            db.query(ReconciliationCase.id)
            .filter(ReconciliationCase.status == "MISMATCH")
            .count()
        ),
        "provider_sync_checkpoints_total": len(checkpoints),
        "provider_sync_pages_total": db.query(ProviderSyncPage.id).count(),
        "provider_sync_checkpoint_oldest_update_age_seconds": oldest_age_seconds,
        "projection_rows_total": projection_rows,
        "projection_missing_users": max(users_total - projection_rows, 0),
    }


class Timer:
    def __enter__(self):
        self.started = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.duration_ms = (time.perf_counter() - self.started) * 1000
