"""
Admin console API (P7-4): global model catalog, circuit breakers.

- ``GET /models`` — the gateway's process-wide model facts catalog
  (``default_catalog``), with process-scoped enablement overrides applied.
- ``PUT /models/{model_id}`` — toggle a deployment's enablement status
  (process-scoped, like the singleton catalog itself; per-tenant
  enablement lives in the tenant-scoped ``/tenants/{id}/models`` routes).
- ``GET /gateway/circuit-breakers`` — per-deployment cooldown state from
  the Redis-shared ``RedisCooldownCache`` (Arch 10, P3-4).
- ``POST /gateway/circuit-breakers/reset`` — clear cooldown state.

``last_failure_at`` is not recorded by the cooldown cache; it is returned
as null rather than fabricated.
"""

from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from backend.app.api.dependencies.auth import (
    ApiKeyPrincipal,
    require_mfa_proof,
    require_permission,
)
from backend.app.gateway.catalog import get_default_catalog
from backend.app.gateway.service import get_gateway
from backend.app.infrastructure.db import AuditRepository, get_database_manager
from backend.app.modules.model_catalog import (
    get_global_status,
    set_global_status,
)

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["Admin Console"])


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


def _spec_to_entry(spec: Any, status_value: str) -> dict[str, Any]:
    return {
        "id": spec.key,
        "provider": spec.provider,
        "model_id": spec.model,
        "display_name": spec.model,
        "capabilities": [],
        "context_window": spec.context_window,
        "max_output_tokens": 0,
        "input_price_per_1k": spec.input_price_per_1k,
        "output_price_per_1k": spec.output_price_per_1k,
        "status": status_value,
        "tier": 0,
        "region_availability": [],
        "updated_at": "",
    }


@router.get("/models")
async def list_models(
    principal: ApiKeyPrincipal = Depends(require_permission("models:read")),
) -> list[dict[str, Any]]:
    """The process-wide model catalog, with enablement overrides applied.

    ``capabilities``/``max_output_tokens``/``tier``/``region_availability``
    are not tracked by the catalog and are returned empty/0."""
    catalog = get_default_catalog()
    return [
        _spec_to_entry(spec, get_global_status(spec.provider, spec.model) or "active")
        for spec in catalog.list_all()
    ]


class ModelStatusUpdate(BaseModel):
    status: str = Field(..., pattern="^(active|disabled|deprecated)$")


@router.put("/models/{model_id}")
async def update_model(
    model_id: str,
    request: ModelStatusUpdate,
    principal: ApiKeyPrincipal = Depends(require_permission("models:write")),
    _mfa: ApiKeyPrincipal = Depends(require_mfa_proof),
) -> dict[str, Any]:
    """Set a deployment's enablement status (process-scoped)."""
    if ":" not in model_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="model_id must be 'provider:model'",
        )
    provider, model = model_id.split(":", 1)
    catalog = get_default_catalog()
    spec = catalog.get(provider, model)
    if spec is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Model not found: {model_id}",
        )
    set_global_status(provider, model, request.status)
    await AuditRepository(get_database_manager()).add(
        action="model.status_changed",
        resource_type="model",
        resource_id=model_id,
        tenant_id=None,
        actor_type="api_key",
        actor_id=principal.key_id,
        details={"provider": provider, "model": model, "status": request.status},
    )
    logger.info(
        "model_status_changed",
        model_id=model_id,
        status=request.status,
        actor=principal.key_id,
    )
    return _spec_to_entry(spec, request.status)


# ---------------------------------------------------------------------------
# Circuit breakers
# ---------------------------------------------------------------------------


@router.get("/gateway/circuit-breakers")
async def list_circuit_breakers(
    principal: ApiKeyPrincipal = Depends(require_permission("models:read")),
) -> list[dict[str, Any]]:
    """Per-deployment cooldown state. Every catalog deployment is listed;
    deployments without a failure record are closed. Redis down returns
    the catalog with everything closed (cooldown checks fail open)."""
    import time

    gateway = get_gateway()
    cooldowns = gateway.cooldowns
    snapshot = await cooldowns.snapshot()
    allowed_fails = cooldowns.config.allowed_fails

    entries: list[dict[str, Any]] = []
    for spec in get_default_catalog().list_all():
        key = spec.key
        count = snapshot.get(key, 0)
        if count <= 0:
            entries.append(
                {
                    "provider": spec.provider,
                    "model": spec.model,
                    "state": "closed",
                    "failure_count": 0,
                    "failure_rate": 0.0,
                    "last_failure_at": None,
                    "cooldown_until": None,
                }
            )
            continue

        ttl = await cooldowns.cooldown_ttl(spec.provider, spec.model)
        if count < allowed_fails:
            state = "closed"
        elif ttl and ttl > 0:
            state = "open"
        else:
            state = "half_open"
        entries.append(
            {
                "provider": spec.provider,
                "model": spec.model,
                "state": state,
                "failure_count": count,
                "failure_rate": round(min(count / allowed_fails, 1.0), 4),
                "last_failure_at": None,
                "cooldown_until": (
                    (time.time() + ttl) if ttl and ttl > 0 else None
                ),
            }
        )
    return entries


class CircuitBreakerResetRequest(BaseModel):
    provider: str | None = Field(default=None, max_length=32)
    model: str | None = Field(default=None, max_length=128)


@router.post("/gateway/circuit-breakers/reset")
async def reset_circuit_breakers(
    request: CircuitBreakerResetRequest,
    principal: ApiKeyPrincipal = Depends(require_permission("models:write")),
    _mfa: ApiKeyPrincipal = Depends(require_mfa_proof),
) -> dict[str, Any]:
    """Clear cooldown state: one deployment (provider+model), one provider,
    or all deployments when neither is given."""
    cleared = await get_gateway().cooldowns.reset(request.provider, request.model)
    await AuditRepository(get_database_manager()).add(
        action="circuit_breakers.reset",
        resource_type="gateway",
        resource_id="cooldowns",
        tenant_id=None,
        actor_type="api_key",
        actor_id=principal.key_id,
        details={"provider": request.provider, "model": request.model, "cleared": cleared},
    )
    logger.info(
        "circuit_breakers_reset",
        provider=request.provider,
        model=request.model,
        cleared=cleared,
        actor=principal.key_id,
    )
    return {"cleared": cleared}
