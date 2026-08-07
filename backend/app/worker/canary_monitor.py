"""
P6-5 — Config canary pipeline, operations side (Arch §13).

Extends the deterministic canary bucketing from ``governance/promotion.py``
with the runtime monitoring and auto-rollback mechanism:

  1. When a config version is promoted with ``canary_percent < 100``, the
     canary monitor tracks error rates and guardrail block rates for traffic
     routed to the canary vs the baseline.
  2. If the canary's error rate exceeds a configurable regression threshold
     relative to the baseline, the monitor triggers an automatic rollback
     by demoting the canary version back to ``superseded``.
  3. The canary may be gradually ramped up (10% → 25% → 50% → 100%) via
     the admin UI (P7-4) or API.

The canary bucketing logic itself lives in ``governance/promotion.py``
(already implemented: ``canary_bucket()``). This module adds the metrics
integration, regression detection, and rollback automation.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

JOB_CANARY_EVALUATE = "canary.evaluate"


@dataclass
class CanaryMetrics:
    """Tracks canary vs baseline performance."""
    requests: int = 0
    errors: int = 0
    guardrail_blocks: int = 0
    total_latency: float = 0.0
    start_time: float = field(default_factory=time.time)

    @property
    def error_rate(self) -> float:
        return self.errors / self.requests if self.requests > 0 else 0.0

    @property
    def block_rate(self) -> float:
        return self.guardrail_blocks / self.requests if self.requests > 0 else 0.0

    @property
    def avg_latency(self) -> float:
        return self.total_latency / self.requests if self.requests > 0 else 0.0

    def record(self, error: bool = False, blocked: bool = False, latency: float = 0.0) -> None:
        self.requests += 1
        if error:
            self.errors += 1
        if blocked:
            self.guardrail_blocks += 1
        self.total_latency += latency

    def to_dict(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "errors": self.errors,
            "error_rate": round(self.error_rate, 4),
            "guardrail_blocks": self.guardrail_blocks,
            "block_rate": round(self.block_rate, 4),
            "avg_latency": round(self.avg_latency, 4),
        }


@dataclass
class CanaryRollout:
    """State of an active canary rollout for a tenant config version."""
    tenant_id: str
    canary_version: int
    baseline_version: int
    canary_percent: int
    baseline_metrics: CanaryMetrics = field(default_factory=CanaryMetrics)
    canary_metrics: CanaryMetrics = field(default_factory=CanaryMetrics)
    # Regression thresholds (canary error rate must not exceed baseline by more than this)
    error_rate_threshold: float = 0.05  # 5% absolute difference
    block_rate_threshold: float = 0.10  # 10% absolute difference
    min_samples: int = 50  # minimum requests before evaluating
    rolled_back: bool = False
    promoted_full: bool = False

    def record_request(
        self,
        is_canary: bool,
        error: bool = False,
        blocked: bool = False,
        latency: float = 0.0,
    ) -> None:
        """Record a request outcome for canary or baseline."""
        target = self.canary_metrics if is_canary else self.baseline_metrics
        target.record(error=error, blocked=blocked, latency=latency)

    def evaluate(self) -> tuple[bool, str | None]:
        """Evaluate whether the canary is regressing.

        Returns ``(should_rollback, reason)``.
        Only evaluates after ``min_samples`` have been collected for both.
        """
        if self.rolled_back or self.promoted_full:
            return False, None

        if (self.canary_metrics.requests < self.min_samples or
                self.baseline_metrics.requests < self.min_samples):
            return False, None

        # Error rate regression
        error_diff = self.canary_metrics.error_rate - self.baseline_metrics.error_rate
        if error_diff > self.error_rate_threshold:
            reason = (
                f"canary error rate {self.canary_metrics.error_rate:.4f} exceeds "
                f"baseline {self.baseline_metrics.error_rate:.4f} by "
                f"{error_diff:.4f} (threshold {self.error_rate_threshold})"
            )
            return True, reason

        # Block rate regression
        block_diff = self.canary_metrics.block_rate - self.baseline_metrics.block_rate
        if block_diff > self.block_rate_threshold:
            reason = (
                f"canary block rate {self.canary_metrics.block_rate:.4f} exceeds "
                f"baseline {self.baseline_metrics.block_rate:.4f} by "
                f"{block_diff:.4f} (threshold {self.block_rate_threshold})"
            )
            return True, reason

        return False, None

    def to_dict(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "canary_version": self.canary_version,
            "baseline_version": self.baseline_version,
            "canary_percent": self.canary_percent,
            "baseline": self.baseline_metrics.to_dict(),
            "canary": self.canary_metrics.to_dict(),
            "rolled_back": self.rolled_back,
            "promoted_full": self.promoted_full,
        }


# Active canary rollouts (in-memory; persisted to DB via audit events)
_active_rollouts: dict[str, CanaryRollout] = {}


def start_canary(
    tenant_id: str,
    canary_version: int,
    baseline_version: int,
    canary_percent: int,
    error_rate_threshold: float = 0.05,
    block_rate_threshold: float = 0.10,
    min_samples: int = 50,
) -> CanaryRollout:
    """Start a canary rollout for a tenant config version."""
    rollout = CanaryRollout(
        tenant_id=tenant_id,
        canary_version=canary_version,
        baseline_version=baseline_version,
        canary_percent=canary_percent,
        error_rate_threshold=error_rate_threshold,
        block_rate_threshold=block_rate_threshold,
        min_samples=min_samples,
    )
    _active_rollouts[tenant_id] = rollout
    logger.info(
        "canary_started",
        tenant_id=tenant_id,
        canary_version=canary_version,
        baseline_version=baseline_version,
        canary_percent=canary_percent,
    )
    return rollout


def get_active_canary(tenant_id: str) -> CanaryRollout | None:
    """Return the active canary rollout for a tenant, if any."""
    return _active_rollouts.get(tenant_id)


def record_outcome(
    tenant_id: str,
    request_key: str | None = None,
    *,
    error: bool = False,
    blocked: bool = False,
    latency_ms: float = 0.0,
) -> None:
    """Record one request outcome against the tenant's active canary.

    The request bucket is recomputed deterministically (``canary_bucket``
    with the same request key used for config resolution), so a request
    is attributed to the same slice it was actually served from. No
    active rollout or unrelated tenants are no-ops.
    """
    rollout = _active_rollouts.get(tenant_id)
    if rollout is None:
        return
    from backend.app.governance.promotion import canary_bucket

    is_canary = canary_bucket(request_key, rollout.canary_percent)
    rollout.record_request(
        is_canary,
        error=error,
        blocked=blocked,
        latency=latency_ms / 1000.0,
    )


def end_canary(tenant_id: str, rolled_back: bool = False) -> None:
    """End a canary rollout (either promoted to 100% or rolled back)."""
    rollout = _active_rollouts.pop(tenant_id, None)
    if rollout:
        if rolled_back:
            rollout.rolled_back = True
        else:
            rollout.promoted_full = True
        logger.info(
            "canary_ended",
            tenant_id=tenant_id,
            rolled_back=rolled_back,
            metrics=rollout.to_dict(),
        )


async def handle_canary_evaluate(payload: dict[str, Any]) -> None:
    """Worker job: evaluate all active canary rollouts for regressions.

    Auto-rolls-back any canary that exceeds the regression threshold.
    Scheduled periodically (every 5 minutes by default).
    """
    logger.info("canary_evaluate_started", active_rollouts=len(_active_rollouts))

    for tenant_id, rollout in list(_active_rollouts.items()):
        should_rollback, reason = rollout.evaluate()
        if should_rollback:
            logger.warning(
                "canary_auto_rollback",
                tenant_id=tenant_id,
                canary_version=rollout.canary_version,
                reason=reason,
            )
            try:
                from backend.app.infrastructure.db import (
                    TenantConfigVersionRepository,
                    get_database_manager,
                )

                repo = TenantConfigVersionRepository(get_database_manager())
                await repo.auto_rollback(
                    tenant_id,
                    rollout.canary_version,
                    reason=reason or "canary regression",
                    rolled_back_by="canary-monitor",
                )
            except Exception as e:
                logger.error(
                    "canary_rollback_db_failed",
                    tenant_id=tenant_id,
                    version=rollout.canary_version,
                    error=str(e),
                )
            end_canary(tenant_id, rolled_back=True)

            try:
                from backend.app.infrastructure.observability.metrics import get_metrics
                get_metrics().errors_total.labels(
                    tenant_id=tenant_id, error_type="canary_rollback"
                ).inc()
            except Exception:
                pass
        else:
            logger.info(
                "canary_healthy",
                tenant_id=tenant_id,
                canary=rollout.canary_metrics.to_dict(),
                baseline=rollout.baseline_metrics.to_dict(),
            )

    logger.info("canary_evaluate_completed")
