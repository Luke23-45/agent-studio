"""
Tool-result clearing (Arch 8.3, P2-7).

A sub-transcript operation: superseded, re-fetchable tool results are
replaced with short placeholders -- the ``tool_use`` record survives, the
payload is dropped. The durable reclaim runs as a background
``tool_result.clear`` job (never on the request path); rendered context
additionally swaps live tool payloads for the placeholder when the tenant
enables the feature, so stale payloads cannot bloat what the model sees.
"""

from dataclasses import dataclass
from typing import Any

from backend.app.context import TOOL_RESULT_PLACEHOLDER
from backend.app.domain.tenant import TenantConfig
from backend.app.infrastructure.db.threads import ThreadRepository

JOB_TOOL_RESULT_CLEAR = "tool_result.clear"

#: Tenant feature flag that enables clearing (features["clear_tool_results"]).
TOOL_CLEAR_FEATURE = "clear_tool_results"

#: Default newest-turns guard (tool_clearing["keep_recent_turns"]).
DEFAULT_KEEP_RECENT_TURNS = 2


@dataclass
class ToolClearingConfig:
    enabled: bool = False
    keep_recent_turns: int = DEFAULT_KEEP_RECENT_TURNS


def tool_clearing_config(tenant_config: TenantConfig) -> ToolClearingConfig:
    """Derive the per-tenant clearing settings (feature-gated)."""
    return ToolClearingConfig(
        enabled=bool(tenant_config.features.get(TOOL_CLEAR_FEATURE, False)),
        keep_recent_turns=int(
            tenant_config.tool_clearing.get(
                "keep_recent_turns", DEFAULT_KEEP_RECENT_TURNS
            )
        ),
    )


async def clear_stale_tool_results(
    db: Any,
    tenant_id: str,
    thread_id: str,
    *,
    tenant_config: TenantConfig | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Reclaim stale tool-result payloads for one thread (P2-7).

    Feature-gated: tenants without ``features["clear_tool_results"]`` are a
    no-op. ``keep_recent_turns`` (tenant ``tool_clearing``) keeps the newest
    turns intact so the in-flight conversation stays fully legible.
    Missing tenants/threads degrade to a reason dict -- a background job
    never raises for an absent record.
    """
    if tenant_config is None:
        from backend.app.application.compaction.refresh import (
            load_effective_tenant_config,
        )
        from backend.app.infrastructure.db.repositories import TenantRepository

        tenant_row = await TenantRepository(db).get_by_id(tenant_id)
        if not tenant_row:
            return {"cleared": 0, "reason": "tenant_not_found"}
        tenant_config = await load_effective_tenant_config(db, tenant_row)

    config = tool_clearing_config(tenant_config)
    if not config.enabled:
        return {"cleared": 0, "reason": "disabled"}

    threads = ThreadRepository(db)
    if await threads.get_thread(tenant_id, thread_id) is None:
        return {"cleared": 0, "reason": "thread_not_found"}

    result = await threads.clear_tool_results(
        tenant_id,
        thread_id,
        keep_recent_turns=config.keep_recent_turns,
        placeholder=TOOL_RESULT_PLACEHOLDER,
        request_id=request_id,
    )
    return {
        **result,
        "reason": "cleared" if result["cleared"] else "nothing_to_clear",
    }
