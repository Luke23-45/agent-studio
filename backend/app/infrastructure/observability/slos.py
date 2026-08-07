"""
P6-3 — SLO definitions and error-budget tracking (Arch §13).

Defines the platform SLOs as code.  These are consumed by:
  - ``ops/monitoring/alert_rules.yml`` (Prometheus alerting rules)
  - The health endpoint (readiness degrades when budgets are exhausted)
  - The admin UI (P7-4) for real-time SLO dashboards

SLOs are expressed as objectives on the metrics defined in ``metrics.py``.
Error budgets track the remaining allowable failure percentage over a
rolling window.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class SLODefinition:
    """A single SLO objective."""
    name: str
    description: str
    metric: str                   # Prometheus metric name
    threshold: float              # target value
    comparator: str = "le"        # le (latency) | ge (availability)
    window_seconds: int = 3600    # rolling window (1h default)
    error_budget_percent: float = 0.5  # 0.5% = 99.5% SLO
    labels: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Platform SLOs (Arch §13)
# ---------------------------------------------------------------------------
PLATFORM_SLOS: list[SLODefinition] = [
    SLODefinition(
        name="ttft_p95",
        description="95th percentile time-to-first-token under 2 seconds",
        metric="neryva_ttft_seconds",
        threshold=2.0,
        comparator="le",
        window_seconds=3600,
        error_budget_percent=0.5,
    ),
    SLODefinition(
        name="e2e_p95",
        description="95th percentile end-to-end latency under 1.5 seconds",
        metric="neryva_request_duration_seconds",
        threshold=1.5,
        comparator="le",
        window_seconds=3600,
        error_budget_percent=0.5,
    ),
    SLODefinition(
        name="stream_completion_rate",
        description="Stream completion rate above 99.5%",
        metric="neryva_stream_completion_total",
        threshold=0.995,
        comparator="ge",
        window_seconds=3600,
        error_budget_percent=0.5,
    ),
    SLODefinition(
        name="error_rate",
        description="Error rate below 0.5%",
        metric="neryva_errors_total",
        threshold=0.005,
        comparator="le",
        window_seconds=3600,
        error_budget_percent=0.5,
    ),
]


@dataclass
class ErrorBudget:
    """Tracks remaining error budget for an SLO over a rolling window."""
    slo: SLODefinition
    total_requests: int = 0
    failed_requests: int = 0
    window_start: float = field(default_factory=time.time)

    @property
    def budget_total(self) -> float:
        """Total allowed failures in the window."""
        if self.total_requests == 0:
            return 0.0
        return self.total_requests * (self.slo.error_budget_percent / 100.0)

    @property
    def budget_remaining(self) -> float:
        """Remaining failure budget."""
        return max(0.0, self.budget_total - self.failed_requests)

    @property
    def budget_remaining_percent(self) -> float:
        """Remaining budget as a percentage of total budget."""
        if self.budget_total == 0:
            return 100.0
        return (self.budget_remaining / self.budget_total) * 100.0

    @property
    def is_exhausted(self) -> bool:
        return self.budget_remaining <= 0 and self.total_requests > 0

    def record(self, success: bool) -> None:
        """Record a request outcome."""
        self.total_requests += 1
        if not success:
            self.failed_requests += 1

    def reset_if_window_expired(self) -> None:
        """Reset counters if the rolling window has elapsed."""
        now = time.time()
        if now - self.window_start >= self.slo.window_seconds:
            self.total_requests = 0
            self.failed_requests = 0
            self.window_start = now

    def to_dict(self) -> dict[str, Any]:
        return {
            "slo": self.slo.name,
            "total_requests": self.total_requests,
            "failed_requests": self.failed_requests,
            "budget_total": round(self.budget_total, 2),
            "budget_remaining": round(self.budget_remaining, 2),
            "budget_remaining_percent": round(self.budget_remaining_percent, 2),
            "is_exhausted": self.is_exhausted,
            "window_seconds": self.slo.window_seconds,
        }


class SLOTracker:
    """Tracks error budgets for all platform SLOs.

    Used by the health endpoint and admin UI to surface SLO status.
    The Prometheus-level alerting is handled by ``alert_rules.yml``;
    this tracker provides the application-level view.
    """

    def __init__(self, slos: list[SLODefinition] | None = None) -> None:
        self._slos = slos or PLATFORM_SLOS
        self._budgets: dict[str, ErrorBudget] = {
            slo.name: ErrorBudget(slo=slo) for slo in self._slos
        }

    def record(self, slo_name: str, success: bool) -> None:
        """Record a request outcome for a specific SLO."""
        budget = self._budgets.get(slo_name)
        if budget:
            budget.reset_if_window_expired()
            budget.record(success)

            if budget.is_exhausted:
                logger.warning(
                    "slo_budget_exhausted",
                    slo=slo_name,
                    failed=budget.failed_requests,
                    total=budget.total_requests,
                )

    def get_status(self) -> dict[str, dict[str, Any]]:
        """Return current SLO status for all tracked objectives."""
        result: dict[str, dict[str, Any]] = {}
        for name, budget in self._budgets.items():
            budget.reset_if_window_expired()
            result[name] = budget.to_dict()
        return result

    def any_exhausted(self) -> bool:
        """True if any SLO error budget is exhausted."""
        return any(b.is_exhausted for b in self._budgets.values())


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------
_slo_tracker: SLOTracker | None = None


def init_slo_tracker(slos: list[SLODefinition] | None = None) -> SLOTracker:
    global _slo_tracker
    if _slo_tracker is None:
        _slo_tracker = SLOTracker(slos)
        logger.info("slo_tracker_initialized", slos=[s.name for s in (slos or PLATFORM_SLOS)])
    return _slo_tracker


def get_slo_tracker() -> SLOTracker:
    global _slo_tracker
    if _slo_tracker is None:
        _slo_tracker = SLOTracker()
    return _slo_tracker
