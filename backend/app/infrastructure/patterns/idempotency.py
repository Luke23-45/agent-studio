"""Idempotency keys for write endpoints (matrix 1.7).

A client sends ``Idempotency-Key`` on a write; the server stores the
first response (status code + body) under that key for a TTL and replays
it verbatim for repeat requests. The store is the shared cache manager,
so it is distributed across workers (Redis) with an in-memory fallback.
"""

import hashlib
import structlog
from typing import Any

from backend.app.infrastructure.cache import get_cache_manager

logger = structlog.get_logger(__name__)

IDEMPOTENCY_TTL_SECONDS = 86400


class IdempotencyGuard:
    """Per-key response memoization for write endpoints."""

    def __init__(self, ttl_seconds: int = IDEMPOTENCY_TTL_SECONDS):
        self.ttl_seconds = ttl_seconds

    @staticmethod
    def _key(idempotency_key: str, scope: str = "") -> str:
        digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        return f"idem:{scope}:{digest}" if scope else f"idem:{digest}"

    async def get_response(self, idempotency_key: str, scope: str = "") -> dict[str, Any] | None:
        """Return the stored ``{"status_code", "body"}`` or None."""
        cache = get_cache_manager()
        value = await cache.get(self._key(idempotency_key, scope))
        if value is None:
            return None
        if not isinstance(value, dict) or "status_code" not in value:
            await cache.delete(self._key(idempotency_key, scope))
            return None
        return value

    async def store_response(
        self,
        idempotency_key: str,
        status_code: int,
        body: Any,
        scope: str = "",
    ) -> None:
        cache = get_cache_manager()
        await cache.set(
            self._key(idempotency_key, scope),
            {"status_code": status_code, "body": body},
            ttl=self.ttl_seconds,
        )


_idempotency_guard: IdempotencyGuard | None = None


def get_idempotency_guard() -> IdempotencyGuard:
    global _idempotency_guard
    if _idempotency_guard is None:
        _idempotency_guard = IdempotencyGuard()
    return _idempotency_guard
