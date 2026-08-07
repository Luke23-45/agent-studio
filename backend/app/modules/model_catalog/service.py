"""Per-tenant model catalog: allowlist, fallbacks, cost ceilings (matrix 5.2).

- ``resolve_model`` enforces the allowlist and returns the tenant's best
  enabled (provider, model) pair, falling back in ``fallback_order``.
- ``check_cost_ceiling`` rejects a request when the tenant's cumulative
  estimated cost for the model exceeds the configured ceiling.
- A tenant with an empty catalog is unconstrained (legacy behavior).
"""

import structlog
from typing import Any

from backend.app.gateway.catalog import model_key
from backend.app.infrastructure.db import DatabaseManager, ModelCatalogRepository

logger = structlog.get_logger(__name__)

# Process-wide enablement overrides for the console models tab (P7-4).
# Mirrors the gateway's process-wide ``default_catalog``: in-memory by
# design, so a restart resets to the seed catalog (all active). Per-tenant
# enablement is persisted in ``model_catalog`` rows via the tenant-scoped
# routes and survives restarts.
_GLOBAL_STATUS: dict[str, str] = {}


def set_global_status(provider: str, model: str, status: str) -> str:
    """Set the process-wide enablement status for a deployment."""
    _GLOBAL_STATUS[model_key(provider, model)] = status
    return status


def get_global_status(provider: str, model: str) -> str | None:
    """The process-wide override status for a deployment, if any."""
    return _GLOBAL_STATUS.get(model_key(provider, model))


class ModelNotAllowed(Exception):
    def __init__(self, provider: str, model: str, tenant_id: str):
        self.provider = provider
        self.model = model
        super().__init__(
            f"model '{provider}/{model}' is not in tenant '{tenant_id}' catalog"
        )


class CostCeilingExceeded(Exception):
    def __init__(self, provider: str, model: str, ceiling: float, used: float):
        self.provider = provider
        self.model = model
        super().__init__(
            f"cost ceiling exceeded for '{provider}/{model}': "
            f"used {used:.4f} of {ceiling:.4f} (USD, rolling window)"
        )


ESTIMATE_CHARS_PER_TOKEN = 4.0


class ModelCatalogService:
    """Tenant-scoped model governance."""

    def __init__(self, db: DatabaseManager | None = None, cache: Any | None = None):
        from backend.app.infrastructure.db import get_database_manager
        from backend.app.infrastructure.cache import get_cache_manager

        self.db = db or get_database_manager()
        self.cache = cache or get_cache_manager()
        self.repository = ModelCatalogRepository(self.db)

    async def list_catalog(self, tenant_id: str) -> list[dict[str, Any]]:
        return await self.repository.list_by_tenant(tenant_id)

    async def add_entry(
        self,
        tenant_id: str,
        provider: str,
        model: str,
        cost_ceiling_per_1k: float = 0.0,
        fallback_order: int = 0,
    ) -> dict[str, Any]:
        return await self.repository.create(
            tenant_id, provider, model, cost_ceiling_per_1k, fallback_order
        )

    async def remove_entry(self, entry_id: str, tenant_id: str) -> bool:
        return await self.repository.delete(entry_id, tenant_id)

    async def set_enabled(self, entry_id: str, tenant_id: str, enabled: bool) -> dict[str, Any] | None:
        return await self.repository.set_enabled(entry_id, tenant_id, enabled)

    async def resolve_model(
        self,
        tenant_id: str,
        provider: str,
        model: str,
    ) -> tuple[str, str]:
        """Return the (provider, model) to use for the tenant.

        The requested pair must be in the catalog; otherwise the tenant's
        default is the first enabled entry by fallback order. An empty
        catalog means unconstrained.
        """
        entries = await self.repository.list_by_tenant(tenant_id)
        if not entries:
            return provider, model

        enabled = [e for e in entries if e["enabled"]]
        requested = next(
            (
                e for e in enabled
                if e["provider"] == provider and e["model"] == model
            ),
            None,
        )
        if requested:
            return provider, model

        fallback = min(enabled, key=lambda e: e["fallback_order"]) if enabled else None
        if fallback is None:
            raise ModelNotAllowed(provider, model, tenant_id)
        logger.info(
            "model_catalog_fallback",
            tenant_id=tenant_id,
            requested=f"{provider}/{model}",
            used=f"{fallback['provider']}/{fallback['model']}",
        )
        return fallback["provider"], fallback["model"]

    async def check_cost_ceiling(
        self,
        tenant_id: str,
        provider: str,
        model: str,
        estimated_tokens: int,
        window_seconds: int = 86400,
    ) -> None:
        """Reject the request when cumulative cost exceeds the entry ceiling.

        Cost is estimated as ``tokens / 1000 * ceiling``; usage is counted
        in a rolling TTL window (cache-backed, so distributed with Redis).
        """
        entries = await self.repository.list_by_tenant(tenant_id)
        entry = next(
            (e for e in entries if e["enabled"] and e["provider"] == provider and e["model"] == model),
            None,
        )
        if entry is None or entry["cost_ceiling_per_1k"] <= 0:
            return

        cache_key = f"modelcost:{tenant_id}:{provider}:{model}"
        increment = estimated_tokens / 1000.0 * entry["cost_ceiling_per_1k"]

        cache = self.cache
        if hasattr(cache, "increment") and await cache.increment(cache_key, int(increment * 1000)) is not None:
            used_raw = await cache.get(cache_key, 0) or 0
            used = used_raw / 1000.0
            if used > entry["cost_ceiling_per_1k"]:
                raise CostCeilingExceeded(provider, model, entry["cost_ceiling_per_1k"], used)
            await cache.set(cache_key, used_raw, ttl=window_seconds)
        else:
            # In-memory fallback: single-process accounting only.
            used_raw = await cache.get(cache_key, 0) or 0
            used = (used_raw + increment * 1000) / 1000.0
            if used > entry["cost_ceiling_per_1k"]:
                raise CostCeilingExceeded(provider, model, entry["cost_ceiling_per_1k"], used)
            await cache.set(cache_key, used_raw + increment * 1000, ttl=window_seconds)

    @staticmethod
    def estimate_tokens(*texts: str) -> int:
        total_chars = sum(len(t or "") for t in texts)
        return max(1, int(total_chars / ESTIMATE_CHARS_PER_TOKEN))


def estimate_tokens(*texts: str) -> int:
    """Module-level alias for the static estimator."""
    return ModelCatalogService.estimate_tokens(*texts)
