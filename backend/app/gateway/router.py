"""
Routing (Arch 10, P3-2).

Decision under ~30ms from per-deployment health (Redis cooldown state),
price (catalog price cards) and latency (EWMA tracker) tables, per-tenant
strategy: cost / latency / quality-pinned / pinned. Tiered routing sends
simple queries to the cheap model and complex ones to the capable model
under tenant policy. The router is pure and in-process (no I/O): the
decision cost is bounded by a small candidate list, so the 30ms budget is
trivially met; Redis cooldowns are the authoritative health signal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from backend.app.gateway.catalog import ModelCatalog
from backend.app.gateway.types import GatewayRequest, RouteDecision

# Fallback defaults per provider family when the tenant config carries no
# fallback list: the cheapest shipping model from the catalog seed.
_DEFAULT_FALLBACKS: dict[str, str] = {
    "openai": "gpt-4o-mini",
    "anthropic": "claude-3-5-haiku",
    "google": "gemini-2.0-flash",
    "azure": "gpt-4o-mini",
    "custom": "gpt-4o-mini",
}

# Strategies: stable names used in tenant config and GatewayRequest.
STRATEGY_COST = "cost"
STRATEGY_LATENCY = "latency"
STRATEGY_QUALITY = "quality"
STRATEGY_PINNED = "pinned"

_VALID_STRATEGIES = frozenset(
    {STRATEGY_COST, STRATEGY_LATENCY, STRATEGY_QUALITY, STRATEGY_PINNED}
)


def is_valid_strategy(value: str) -> bool:
    return value in _VALID_STRATEGIES


@dataclass
class LatencyTracker:
    """In-process EWMA latency per deployment key (``provider:model``)."""

    alpha: float = 0.3
    _latencies: dict[str, float] = field(default_factory=dict)

    def update(self, provider: str, model: str, latency_ms: float) -> None:
        key = f"{provider}:{model}"
        prev = self._latencies.get(key)
        if prev is None:
            self._latencies[key] = latency_ms
        else:
            self._latencies[key] = self.alpha * latency_ms + (1.0 - self.alpha) * prev

    def get(self, provider: str, model: str) -> float:
        return self._latencies.get(f"{provider}:{model}", 0.0)

    def snapshot(self) -> dict[str, float]:
        return dict(self._latencies)


def deployment_key(provider: str, model: str) -> str:
    return f"{provider.strip().lower()}:{model.strip().lower()}"


def tenant_fallback_candidates(gateway_cfg: dict) -> list[str]:
    """``gateway.fallback_models`` entries (``provider:model`` strings)."""
    raw = (gateway_cfg or {}).get("fallback_models") or []
    return [str(c).strip() for c in raw if str(c).strip()]


def _default_model_for(catalog: ModelCatalog, provider: str) -> str:
    """A sensible default model when the request carries none."""
    defaults = {
        "openai": "gpt-4o-mini",
        "anthropic": "claude-3-5-sonnet",
        "google": "gemini-2.0-flash",
        "azure": "gpt-4o-mini",
        "custom": "gpt-4o-mini",
    }
    model = defaults.get(provider)
    if model and catalog.get(provider, model):
        return model
    for spec in catalog.list_all():
        if spec.provider == provider:
            return spec.model
    return defaults.get(provider, "gpt-4o-mini")


def _split_deployment(entry: str) -> tuple[str, str]:
    if ":" in entry:
        provider, model = entry.split(":", 1)
        return provider.strip().lower(), model.strip()
    return entry.strip().lower(), entry.strip()


class Router:
    """Picks the deployment for a request under the tenant's strategy.

    ``availability`` is a callable ``(provider, model) -> bool`` (the
    Redis cooldown cache); candidates whose deployment is cooling down are
    skipped. All inputs are plain data: the router performs no I/O.
    """

    def __init__(
        self,
        catalog: ModelCatalog,
        latency_tracker: LatencyTracker | None = None,
        *,
        cheap_model_fallback: str = "gpt-4o-mini",
    ):
        self.catalog = catalog
        self.latency = latency_tracker or LatencyTracker()
        self.cheap_model_fallback = cheap_model_fallback

    def candidates(
        self,
        request: GatewayRequest,
        gateway_cfg: dict | None = None,
    ) -> list[tuple[str, str]]:
        """Ranked candidate deployments for the request (deduped, ordered).

        Order reflects the tenant's strategy: for cost/latency the list is
        sorted by score; for quality/pinned the preferred target stays
        first. Tiering (Arch 10) puts the cheap model first for simple
        queries so cost scoring picks it.
        """
        cfg = gateway_cfg or {}
        strategy = request.strategy if is_valid_strategy(request.strategy) else STRATEGY_COST

        if request.pinned:
            return [_split_deployment(request.pinned)]

        preferred: tuple[str, str] = (
            request.provider,
            request.model or _default_model_for(self.catalog, request.provider),
        )
        candidates: list[tuple[str, str]] = [preferred]

        # Tiered routing: simple queries are served by the cheap model.
        if request.tiered and request.simple_query:
            cheap = str(cfg.get("cheap_model") or self.cheap_model_fallback)
            candidates.append(
                _split_deployment(cheap) if ":" in cheap else (request.provider, cheap)
            )

        # Tenant-configured fallbacks + provider-family defaults.
        for entry in tenant_fallback_candidates(cfg):
            candidate = _split_deployment(entry)
            if candidate not in candidates:
                candidates.append(candidate)
        default_fallback = _DEFAULT_FALLBACKS.get(request.provider)
        if default_fallback and (request.provider, default_fallback) not in candidates:
            candidates.append((request.provider, default_fallback))

        if strategy in (STRATEGY_COST, STRATEGY_LATENCY) and len(candidates) > 1:
            candidates = sorted(
                candidates, key=lambda c: self._score(c, request, strategy)
            )
        return candidates

    def decide(
        self,
        request: GatewayRequest,
        availability: Callable[[str, str], bool],
        gateway_cfg: dict | None = None,
    ) -> RouteDecision:
        """Return the best *available* deployment, or the best one anyway
        (the gateway re-checks availability atomically at call time)."""
        strategy = request.strategy if is_valid_strategy(request.strategy) else STRATEGY_COST
        candidates = self.candidates(request, gateway_cfg)
        for provider, model in candidates:
            if availability(provider, model):
                return RouteDecision(
                    provider=provider,
                    model=model,
                    strategy=strategy,
                    candidates_considered=len(candidates),
                    reason="available",
                )
        provider, model = candidates[0]
        return RouteDecision(
            provider=provider,
            model=model,
            strategy=strategy,
            candidates_considered=len(candidates),
            reason="all_deployments_cooldown",
        )

    def _score(
        self, candidate: tuple[str, str], request: GatewayRequest, strategy: str
    ) -> float:
        provider, model = candidate
        if strategy == STRATEGY_COST:
            cost = self.catalog.estimate_cost(
                provider, model, request.estimated_input_tokens or 1000, 256
            )
            return cost if cost is not None else float("inf")
        if strategy == STRATEGY_LATENCY:
            latency = self.latency.get(provider, model)
            return latency if latency > 0 else float("inf")
        return 0.0  # quality/pinned: keep given order
