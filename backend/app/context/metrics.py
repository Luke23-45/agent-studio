"""
Prompt-cache hit-rate metrics (Arch 8.2 cache discipline, P2-6).

Per-tenant, per-provider counters of provider-reported input/cached tokens.
The hit rate normalizes provider semantics:

- Anthropic: ``input_tokens`` EXCLUDES cache reads -> rate = cached / (input + cached)
- OpenAI family (openai/azure/custom/google): ``prompt_tokens`` INCLUDES
  cached tokens -> rate = cached / input
- unknown providers: Anthropic-style normalization (fallback)

Every ``record`` emits a per-request structlog metric line
(``prompt_cache_hit_rate``) for operators; ``snapshot_all`` exposes the
aggregate for dashboards. Failures never raise into the request path.
"""

from __future__ import annotations

import structlog
from dataclasses import dataclass, field
from typing import Any

logger = structlog.get_logger(__name__)

# OpenAI-family usage reports cached tokens inside prompt_tokens.
_CACHED_INCLUDED_IN_INPUT = ("openai", "azure", "custom", "google")


@dataclass
class TenantCacheCounters:
    """Running totals per (tenant, provider)."""

    requests: int = 0
    input_tokens: int = 0
    cached_tokens: int = 0

    def rate(self, provider: str) -> float:
        """Normalized cache hit rate for this provider (clamped 0..1)."""
        if provider in _CACHED_INCLUDED_IN_INPUT:
            total = self.input_tokens
        else:
            total = self.input_tokens + self.cached_tokens
        if total <= 0:
            return 0.0
        return min(max(self.cached_tokens / total, 0.0), 1.0)


class PromptCacheMetricsCollector:
    """In-process per-tenant hit-rate counters (Redis-shared in P3-4)."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], TenantCacheCounters] = {}

    def record(
        self,
        tenant_id: str,
        provider: str,
        input_tokens: int,
        cached_tokens: int,
    ) -> None:
        """Record one generation's provider-reported usage."""
        key = (tenant_id, provider)
        row = self._rows.setdefault(key, TenantCacheCounters())
        row.requests += 1
        row.input_tokens += max(int(input_tokens), 0)
        row.cached_tokens += max(int(cached_tokens), 0)
        logger.info(
            "prompt_cache_hit_rate",
            tenant_id=tenant_id,
            provider=provider,
            requests=row.requests,
            input_tokens=row.input_tokens,
            cached_tokens=row.cached_tokens,
            hit_rate=round(row.rate(provider), 4),
        )

    def snapshot(self, tenant_id: str, provider: str) -> dict[str, Any]:
        """Current counters for one (tenant, provider) pair."""
        row = self._rows.get((tenant_id, provider))
        if row is None:
            return {
                "tenant_id": tenant_id,
                "provider": provider,
                "requests": 0,
                "input_tokens": 0,
                "cached_tokens": 0,
                "hit_rate": 0.0,
            }
        return {
            "tenant_id": tenant_id,
            "provider": provider,
            "requests": row.requests,
            "input_tokens": row.input_tokens,
            "cached_tokens": row.cached_tokens,
            "hit_rate": round(row.rate(provider), 4),
        }

    def snapshot_all(self) -> dict[str, dict[str, Any]]:
        """Per-tenant, per-provider aggregate for dashboards."""
        return {
            f"{tenant_id}:{provider}": self.snapshot(tenant_id, provider)
            for (tenant_id, provider) in sorted(self._rows)
        }

    def reset(self) -> None:
        self._rows.clear()


_collector: PromptCacheMetricsCollector | None = None


def get_prompt_cache_metrics() -> PromptCacheMetricsCollector:
    """Shared in-process collector (per-replica; Redis-shared in P3-4)."""
    global _collector
    if _collector is None:
        _collector = PromptCacheMetricsCollector()
    return _collector
