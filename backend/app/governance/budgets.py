"""
P5-7 — Budget hierarchy integration (Arch §6.3.8, §10).

Composes the four USD budget levels (platform > tenant > surface >
end-user) into the gateway config the orchestration passes down. The
gateway's ``QuotaService`` enforces them via Redis Lua; this module only
*maps* the compiled config into the ``quota_*_usd`` keys the gateway reads,
keeping the hierarchy additive and testable without Redis.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:
    from backend.app.gateway.quota import QuotaLimits


def build_gateway_budget_cfg(
    compiled_budgets: Mapping[str, Any],
) -> dict[str, float]:
    """Compose the gateway-config budget keys from the compiled config.

    ``compiled_budgets`` is the budgets mapping of a P5-1 compiled surface
    config, which already carries ``quota_platform_usd``/``quota_tenant_usd``
    (settings defaults) and ``quota_surface_usd``/``quota_end_user_usd``
    (surface overrides, surface clamped to tenant). Returns the four-key
    dict the gateway reads; 0.0/absent = unlimited at that level.
    """
    return {
        "quota_platform_usd": float(compiled_budgets.get("quota_platform_usd", 0.0)),
        "quota_tenant_usd": float(compiled_budgets.get("quota_tenant_usd", 0.0)),
        "quota_surface_usd": float(compiled_budgets.get("quota_surface_usd", 0.0)),
        "quota_end_user_usd": float(compiled_budgets.get("quota_end_user_usd", 0.0)),
    }


def quota_limits(cfg: Mapping[str, Any]) -> QuotaLimits:
    """Expose the composed limits as a gateway ``QuotaLimits``."""
    from backend.app.gateway.service import quota_limits_from_cfg

    return quota_limits_from_cfg(dict(cfg))


def surface_over_budget_while_tenant_fine(
    *,
    tenant_usd: float,
    surface_usd: float,
    tenant_spent: float,
    surface_spent: float,
    estimated_usd: float,
) -> bool:
    """Pure check mirroring the gateway reserve script: is the request
    blocked at the surface level while the tenant level still has headroom?"""
    if surface_usd <= 0:
        return False
    blocked_by_surface = (surface_spent + estimated_usd) > surface_usd
    blocked_by_tenant = tenant_usd > 0 and (
        tenant_spent + estimated_usd
    ) > tenant_usd
    return blocked_by_surface and not blocked_by_tenant


BUDGET_LEVEL_LABELS: dict[str, str] = {
    "platform": "The platform budget",
    "tenant": "The tenant budget",
    "surface": "This surface's budget",
    "end_user": "This user's budget",
}


def budget_rejection_message(
    level: str,
    limit_usd: float | None = None,
    projected_usd: float | None = None,
) -> str:
    """Human-readable rejection line for the surface-level UI (P5-7).

    A surface blocked while the tenant still has headroom is a *policy*
    signal the customer-facing widget must show distinctly — the request was
    refused by the budget gate, not failed by an outage. The message names
    the offending level (surface vs tenant) so support can act on the right
    budget.
    """
    label = BUDGET_LEVEL_LABELS.get(level, level or "budget")
    limit = f"${limit_usd:.2f}" if limit_usd is not None else "its monthly limit"
    projected = f" (projected ${projected_usd:.2f})" if projected_usd is not None else ""
    return (
        f"{label} has reached {limit}{projected} this month. This request was "
        "rejected. Contact your administrator to raise the budget."
    )