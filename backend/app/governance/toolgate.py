"""
P5-3 — Tool authorization gate (Arch §14, §2.8).

Authorization is separate from verification and fully deterministic: a tool
call is allowed only when it (1) exists in the tenant's tool registry and is
enabled there, and (2) is on the authorized tool allowlist for the request
surface (compiled per-surface allowlist). Denials are audited with a
structured reason so the decision is reconstructable (P5-9).
"""

from __future__ import annotations

import structlog
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from backend.app.domain.tenant import TenantConfig

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ToolAuthorization:
    """Deterministic gate decision + the reason for denial."""

    tool_name: str
    allowed: bool
    reason: str | None = None
    surface_id: str | None = None


class ToolAuthorizationGate:
    """Deterministic per-tool-call authorization bound to one request.

    The gate makes no I/O: all data it reads (tenant registry status +
    compiled surface allowlist) is passed in. The route builds one gate per
    request from repository state; the orchestration loop calls
    ``authorize`` per tool call (sync, pure).
    """

    def __init__(
        self,
        *,
        tenant_config: TenantConfig,
        registered_tools: Mapping[str, bool] | None = None,
        surface_allowlist: Sequence[str] = (),
        surface_id: str | None = None,
        audit: Any = None,
    ):
        self._tenant_id = str(tenant_config.id)
        self._registered = dict(registered_tools or {})
        self._allowlist = set(surface_allowlist or ())
        self._surface_id = surface_id
        self._audit = audit

    def authorize(self, call: Mapping[str, Any]) -> ToolAuthorization:
        name = str(call.get("name") or "")
        if not name:
            return ToolAuthorization(name, False, "missing tool name", self._surface_id)

        enabled = self._registered.get(name, False)
        if not enabled:
            return self._deny(name, "tool not enabled for tenant")
        if name not in self._allowlist:
            return self._deny(name, "tool not in surface allowlist")
        return ToolAuthorization(name, True, surface_id=self._surface_id)

    async def authorize_async(self, call: Mapping[str, Any]) -> ToolAuthorization:
        decision = self.authorize(call)
        if self._audit and not decision.allowed:
            try:
                outcome = self._audit(
                    {
                        "tool": decision.tool_name,
                        "allowed": False,
                        "reason": decision.reason,
                        "surface_id": decision.surface_id,
                        "tenant_id": self._tenant_id,
                    }
                )
                if hasattr(outcome, "__await__"):
                    await outcome
            except Exception as e:  # pragma: no cover - audit never crashes the loop
                logger.warning("tool_denial_audit_failed", error=str(e))
        return decision

    def _deny(self, name: str, reason: str) -> ToolAuthorization:
        return ToolAuthorization(name, False, reason, self._surface_id)


async def build_authorizer(
    *,
    tenant_config: TenantConfig,
    db: Any,
    surface: Mapping[str, Any] | None = None,
    audit_service: Any = None,
) -> ToolAuthorizationGate:
    """Compose the per-request authorization gate from repository state.

    Returns a gate whose ``authorize`` denies everything when the tenant has
    no registered/enabled tools or the surface whitelist is empty (deny by
    default, P0-4).
    """
    from backend.app.infrastructure.db import ToolRegistryRepository

    registered = await ToolRegistryRepository(db).enabled_map(str(tenant_config.id))
    surface_allowlist = []
    if surface:
        surface_allowlist = surface.get("tool_allowlist") or []
    return ToolAuthorizationGate(
        tenant_config=tenant_config,
        registered_tools=registered,
        surface_allowlist=surface_allowlist,
        surface_id=surface["id"] if surface else None,
        audit=audit_service,
    )