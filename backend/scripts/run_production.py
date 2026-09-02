from __future__ import annotations

import os
import sys

from app.deployment import deployment_preflight
from app.security import settings


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise SystemExit(f"{name} must be an integer") from exc
    if value <= 0:
        raise SystemExit(f"{name} must be greater than zero")
    return value


def main() -> int:
    if not settings.is_production:
        print("run_production requires APP_ENV=production", file=sys.stderr)
        return 2

    preflight = deployment_preflight(run_canonical_audit=True)
    if not preflight["ready"]:
        print("deployment preflight failed; refusing to start", file=sys.stderr)
        return 1

    host = os.getenv("API_HOST", "0.0.0.0")
    port = _positive_int("API_PORT", 8000)
    workers = _positive_int("API_WORKERS", 1)
    graceful = _positive_int("GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS", 30)

    argv = [
        sys.executable,
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        host,
        "--port",
        str(port),
        "--workers",
        str(workers),
        "--timeout-graceful-shutdown",
        str(graceful),
    ]
    os.execv(sys.executable, argv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
