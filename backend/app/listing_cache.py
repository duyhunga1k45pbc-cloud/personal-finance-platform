"""Optional Redis cache-aside for the provider external transaction listing.

PostgreSQL stays canonical. Correctness comes from the committed per-connection
revision in the cache key (advanced by database triggers in the writer's own
transaction); the TTL only bounds retention. Every Redis failure falls back to
the database path.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import zlib
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
_CHECKSUM_LEN = 8

stats = {"hit": 0, "miss": 0, "error": 0, "corrupt": 0}
_client: Any = None
_client_url: str | None = None
_skip_until = 0.0
BACKOFF_SECONDS = 5.0


def _config() -> tuple[str | None, int, float]:
    url = os.getenv("REDIS_URL", "").strip() or None
    ttl = int(os.getenv("PROVIDER_LISTING_CACHE_TTL_SECONDS", "60"))
    timeout = float(os.getenv("REDIS_TIMEOUT_SECONDS", "0.1"))
    return url, ttl, timeout


def _redis():
    """Return a Redis client, or None when caching is disabled."""
    global _client, _client_url
    url, _, timeout = _config()
    if url is None or time.monotonic() < _skip_until:
        return None
    if _client is None or _client_url != url:
        import redis

        _client = redis.Redis.from_url(
            url, socket_timeout=timeout, socket_connect_timeout=timeout
        )
        _client_url = url
    return _client


def reset_client() -> None:
    global _client, _client_url, _skip_until
    _client = None
    _client_url = None
    _skip_until = 0.0


def _record_failure(action: str) -> None:
    """Skip Redis briefly after an error so an outage costs almost nothing."""
    global _skip_until
    stats["error"] += 1
    _skip_until = time.monotonic() + BACKOFF_SECONDS
    logger.warning("listing cache %s failed; using database for %.0fs", action, BACKOFF_SECONDS)


def normalize_params(params: dict[str, Any] | None = None) -> str:
    """Canonical text for the listing's query parameters (currently none)."""
    clean = {k: v for k, v in (params or {}).items() if v is not None}
    return json.dumps(clean, sort_keys=True, separators=(",", ":"), default=str)


def cache_key(
    *, user_id: int, connection_id: int, revision: int, params: dict[str, Any] | None = None
) -> str:
    digest = hashlib.sha256(normalize_params(params).encode()).hexdigest()[:16]
    return (
        f"pfp:provlist:s{SCHEMA_VERSION}:u{user_id}:c{connection_id}:r{revision}:p{digest}"
    )


def read_revision(db: Session, connection_id: int) -> int:
    value = db.execute(
        text(
            "SELECT revision FROM provider_listing_revisions "
            "WHERE provider_connection_id = :cid"
        ),
        {"cid": connection_id},
    ).scalar()
    return int(value) if value is not None else 0


def _pack(body: bytes) -> bytes:
    return format(zlib.crc32(body), "08x").encode() + body


def _unpack(blob: bytes) -> bytes | None:
    if len(blob) < _CHECKSUM_LEN + 2:
        return None
    head, body = blob[:_CHECKSUM_LEN], blob[_CHECKSUM_LEN:]
    if head != format(zlib.crc32(body), "08x").encode():
        return None
    if not (body.startswith(b"[") and body.endswith(b"]")):
        return None
    return body


def get_cached(key: str) -> bytes | None:
    client = _redis()
    if client is None:
        return None
    try:
        blob = client.get(key)
    except Exception:
        _record_failure("read")
        return None
    if blob is None:
        stats["miss"] += 1
        return None
    body = _unpack(blob)
    if body is None:
        stats["corrupt"] += 1
        return None
    stats["hit"] += 1
    return body


def put_cached(key: str, body: bytes) -> None:
    client = _redis()
    if client is None:
        return
    _, ttl, _ = _config()
    try:
        client.set(key, _pack(body), ex=ttl)
    except Exception:
        _record_failure("write")


def enabled() -> bool:
    return _config()[0] is not None
