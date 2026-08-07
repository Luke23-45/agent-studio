"""
P5-5 — Isolation primitives (Arch §6.3).

Tenant-context resolution is a single step that yields an immutable
``TenantContext`` carried by downstream components; every store gets a
tenant-scoped key/namespace/prefix derived here. One module, no I/O, so the
naming scheme is consistent and testable across DB, Redis, vector, object
storage and traces.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional


@dataclass(frozen=True)
class TenantContext:
    """Immutable tenant-scope for one request.

    Sidesteps mixing credentials: a request is bound to exactly one
    tenant + surface + end-user; null fields mean "not scoped" (platform
    operations).
    """

    tenant_id: str
    surface_id: Optional[str] = None
    end_user_id: Optional[str] = None

    @classmethod
    def from_principal(
        cls, *, tenant_id: str, surface_id: Optional[str] = None,
        end_user_id: Optional[str] = None
    ) -> "TenantContext":
        return cls(tenant_id=tenant_id, surface_id=surface_id, end_user_id=end_user_id)

    def redis_prefix(self) -> str:
        if self.end_user_id:
            return f"tenant:{self.tenant_id}:end_user:{self.end_user_id}:"
        return f"tenant:{self.tenant_id}:"

    def vector_namespace(self, kb_id: Optional[str] = None) -> str:
        base = f"tenant_{self.tenant_id}"
        if kb_id:
            return f"{base}_kb_{kb_id}"
        return base

    def storage_prefix(self) -> str:
        return f"tenant/{self.tenant_id}/"


def resolve_tenant_context(
    *, tenant_id: str, surface: Optional[Mapping[str, Any]] = None,
    end_user_id: Optional[str] = None
) -> TenantContext:
    """Single mandatory resolution step for the isolation boundary."""
    return TenantContext(
        tenant_id=tenant_id,
        surface_id=surface.get("id") if surface else None,
        end_user_id=end_user_id,
    )


def redis_key(context: TenantContext, *parts: str) -> str:
    return context.redis_prefix() + ":".join(parts)


def vector_namespace_for(context: TenantContext, kb_id: Optional[str] = None) -> str:
    return context.vector_namespace(kb_id)


def storage_key(context: TenantContext, *parts: str) -> str:
    return context.storage_prefix() + "/".join(parts)


def trace_tags(context: TenantContext) -> dict[str, str]:
    """Trace span tags that carry tenant/surface attribution (Arch 6.3.6)."""
    tags = {"tenant_id": context.tenant_id}
    if context.surface_id:
        tags["surface_id"] = context.surface_id
    if context.end_user_id:
        tags["end_user_id"] = context.end_user_id
    return tags