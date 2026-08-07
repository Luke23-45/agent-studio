"""
P6-2 — Prometheus / OTel metrics (Arch §13).

Defines the full metric surface required by the architecture:

  - TTFT (Time To First Token) — histogram, per tenant/model
  - Inter-token latency — histogram, per tenant/model
  - Guardrail hit rates — counter, per tenant/rail/decision
  - Cost per conversation — histogram, per tenant/model
  - Compaction frequency — counter, per tenant/trigger_type
  - Queue depth — gauge (live)
  - DLQ size — gauge (live)
  - Token usage — counter, per tenant/model/direction
  - Cache hit rates — counter, per tenant/cache_type/hit_or_miss
  - Stream completion rate — counter, per tenant/status
  - Request duration (end-to-end) — histogram, per tenant/endpoint
  - Error rate — counter, per tenant/error_type
  - Circuit breaker state — gauge, per provider/model

All metrics are namespaced ``neryva_`` for Prometheus consistency. Labels
always include ``tenant_id`` so per-tenant dashboards (P7-4) work.

The module degrades to no-ops when ``prometheus_client`` is not installed.
"""

from __future__ import annotations

import time
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

_HAS_PROMETHEUS = False
_metrics: NeryvaMetrics | None = None

try:
    from prometheus_client import (
        CONTENT_TYPE_LATEST,
        Counter,
        Gauge,
        Histogram,
        Info,
        generate_latest,
    )
    _HAS_PROMETHEUS = True
except ImportError:
    pass


class NeryvaMetrics:
    """Central registry of all Neryva Prometheus metrics.

    Instantiated once via ``init_metrics()``; retrieved via ``get_metrics()``.
    When prometheus_client is absent, every attribute is a ``_NoOpMetric``
    so instrumented code never needs conditionals.
    """

    def __init__(self) -> None:
        if _HAS_PROMETHEUS:
            self._init_real()
        else:
            self._init_noop()
            logger.warning(
                "prometheus_unavailable",
                reason="prometheus_client not installed; metrics disabled",
            )

    # -----------------------------------------------------------------
    # Real Prometheus metrics
    # -----------------------------------------------------------------
    def _init_real(self) -> None:
        # --- Request lifecycle ---
        self.request_duration = Histogram(
            "neryva_request_duration_seconds",
            "End-to-end request duration",
            labelnames=["tenant_id", "endpoint", "method", "status_code"],
            buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 1.5, 2.5, 5.0, 10.0),
        )

        self.request_total = Counter(
            "neryva_requests_total",
            "Total requests",
            labelnames=["tenant_id", "endpoint", "method", "status_code"],
        )

        # --- LLM / Streaming ---
        self.ttft_seconds = Histogram(
            "neryva_ttft_seconds",
            "Time to first token from LLM",
            labelnames=["tenant_id", "provider", "model"],
            buckets=(0.1, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 5.0, 10.0),
        )

        self.inter_token_latency_seconds = Histogram(
            "neryva_inter_token_latency_seconds",
            "Latency between consecutive tokens in a stream",
            labelnames=["tenant_id", "provider", "model"],
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5),
        )

        self.stream_completion_total = Counter(
            "neryva_stream_completion_total",
            "Completed streams by status",
            labelnames=["tenant_id", "status"],  # status: completed|cancelled|error
        )

        # --- Guardrails ---
        self.guardrail_checks_total = Counter(
            "neryva_guardrail_checks_total",
            "Guardrail evaluations",
            labelnames=["tenant_id", "rail_name", "decision"],  # decision: allow|block|escalate
        )

        # --- Cost ---
        self.cost_per_conversation_usd = Histogram(
            "neryva_cost_per_conversation_usd",
            "USD cost per completed conversation turn",
            labelnames=["tenant_id", "provider", "model"],
            buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0),
        )

        # --- Token usage ---
        self.token_usage_total = Counter(
            "neryva_token_usage_total",
            "Token usage by direction",
            # direction: input|output|reasoning|cached
            labelnames=["tenant_id", "provider", "model", "direction"],
        )

        # --- Compaction ---
        self.compaction_total = Counter(
            "neryva_compaction_total",
            "Compaction events by trigger type",
            labelnames=["tenant_id", "trigger"],  # preemptive|reactive|overflow|background
        )

        self.compaction_duration_seconds = Histogram(
            "neryva_compaction_duration_seconds",
            "Time spent performing compaction",
            labelnames=["tenant_id", "trigger"],
            buckets=(0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0),
        )

        # --- Cache ---
        self.cache_operations_total = Counter(
            "neryva_cache_operations_total",
            "Cache hit/miss by type",
            labelnames=["tenant_id", "cache_type", "result"],  # result: hit|miss
        )

        # --- Queue ---
        self.queue_depth = Gauge(
            "neryva_queue_depth",
            "Current job queue depth",
            labelnames=["queue_name"],
        )

        self.dlq_size = Gauge(
            "neryva_dlq_size",
            "Dead-letter queue size",
            labelnames=["queue_name"],
        )

        # --- Circuit breaker ---
        self.circuit_breaker_state = Gauge(
            "neryva_circuit_breaker_state",
            "Circuit breaker state (0=closed, 1=half-open, 2=open)",
            labelnames=["provider", "model"],
        )

        # --- Errors ---
        self.errors_total = Counter(
            "neryva_errors_total",
            "Error count by type",
            labelnames=["tenant_id", "error_type"],  # timeout|5xx|rate_limit|guardrail|internal
        )

        # --- Admission ---
        self.admission_rejected_total = Counter(
            "neryva_admission_rejected_total",
            "Admission control rejections",
            labelnames=["tenant_id"],
        )

        self.active_generations = Gauge(
            "neryva_active_generations",
            "Currently in-flight LLM generations",
            labelnames=["tenant_id"],
        )

        # --- Quality monitor (P6-4) ---
        self.quality_checks_total = Counter(
            "neryva_quality_checks_total",
            "LLM-judge quality checks by verdict",
            labelnames=["tenant_id", "dimension", "verdict"],  # verdict: pass|fail
        )
        self.quality_degraded_total = Counter(
            "neryva_quality_degraded_total",
            "Degraded turns detected by the quality monitor",
            labelnames=["tenant_id"],
        )
        self.quality_escalated_total = Counter(
            "neryva_quality_escalated_total",
            "Quality escalations emitted",
            labelnames=["tenant_id"],
        )
        self.quality_drift = Gauge(
            "neryva_quality_drift",
            "Quality drift status (0=ok, 1=drift) per tenant",
            labelnames=["tenant_id"],
        )

        # --- Info ---
        self.build_info = Info(
            "neryva_build",
            "Build information",
        )
        self.build_info.info({
            "version": "0.1.0",
            "service": "neryva-studio",
        })

    # -----------------------------------------------------------------
    # No-op fallback
    # -----------------------------------------------------------------
    def _init_noop(self) -> None:
        noop = _NoOpMetric()
        self.request_duration = noop
        self.request_total = noop
        self.ttft_seconds = noop
        self.inter_token_latency_seconds = noop
        self.stream_completion_total = noop
        self.guardrail_checks_total = noop
        self.cost_per_conversation_usd = noop
        self.token_usage_total = noop
        self.compaction_total = noop
        self.compaction_duration_seconds = noop
        self.cache_operations_total = noop
        self.queue_depth = noop
        self.dlq_size = noop
        self.circuit_breaker_state = noop
        self.errors_total = noop
        self.admission_rejected_total = noop
        self.active_generations = noop
        self.quality_checks_total = noop
        self.quality_degraded_total = noop
        self.quality_escalated_total = noop
        self.quality_drift = noop
        self.build_info = noop

    # -----------------------------------------------------------------
    # Convenience methods
    # -----------------------------------------------------------------
    def record_ttft(
        self,
        tenant_id: str,
        provider: str,
        model: str,
        ttft: float,
    ) -> None:
        """Record time-to-first-token for a generation."""
        self.ttft_seconds.labels(
            tenant_id=tenant_id, provider=provider, model=model
        ).observe(ttft)

    def record_token_usage(
        self,
        tenant_id: str,
        provider: str,
        model: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        reasoning_tokens: int = 0,
        cached_tokens: int = 0,
    ) -> None:
        """Record token usage across all directions."""
        labels = {"tenant_id": tenant_id, "provider": provider, "model": model}
        if input_tokens:
            self.token_usage_total.labels(**labels, direction="input").inc(input_tokens)
        if output_tokens:
            self.token_usage_total.labels(**labels, direction="output").inc(output_tokens)
        if reasoning_tokens:
            self.token_usage_total.labels(**labels, direction="reasoning").inc(reasoning_tokens)
        if cached_tokens:
            self.token_usage_total.labels(**labels, direction="cached").inc(cached_tokens)

    def record_guardrail(
        self,
        tenant_id: str,
        rail_name: str,
        decision: str,
    ) -> None:
        """Record a guardrail evaluation decision."""
        self.guardrail_checks_total.labels(
            tenant_id=tenant_id, rail_name=rail_name, decision=decision
        ).inc()

    @contextmanager
    def measure_request(
        self,
        tenant_id: str,
        endpoint: str,
        method: str = "POST",
    ) -> Generator[None]:
        """Context manager to measure request duration and count."""
        start = time.monotonic()
        status = "200"
        try:
            yield
        except Exception:
            status = "500"
            raise
        finally:
            duration = time.monotonic() - start
            self.request_duration.labels(
                tenant_id=tenant_id,
                endpoint=endpoint,
                method=method,
                status_code=status,
            ).observe(duration)
            self.request_total.labels(
                tenant_id=tenant_id,
                endpoint=endpoint,
                method=method,
                status_code=status,
            ).inc()


class _NoOpMetric:
    """Stand-in for any Prometheus metric when the library is absent."""

    def labels(self, **kwargs: Any) -> _NoOpMetric:
        return self

    def inc(self, amount: float = 1) -> None:
        pass

    def dec(self, amount: float = 1) -> None:
        pass

    def set(self, value: float) -> None:
        pass

    def observe(self, amount: float) -> None:
        pass

    def info(self, val: dict[str, str]) -> None:
        pass


def init_metrics() -> NeryvaMetrics:
    """Initialize the global metrics registry. Call once at startup."""
    global _metrics
    if _metrics is None:
        _metrics = NeryvaMetrics()
        logger.info("metrics_initialized", prometheus_available=_HAS_PROMETHEUS)
    return _metrics


def get_metrics() -> NeryvaMetrics:
    """Return the initialized metrics instance. Auto-inits if needed."""
    global _metrics
    if _metrics is None:
        _metrics = NeryvaMetrics()
    return _metrics


def metrics_endpoint_content() -> tuple[bytes, str]:
    """Generate Prometheus-compatible metrics output for the /metrics endpoint.

    Returns ``(body_bytes, content_type)``.
    """
    if _HAS_PROMETHEUS:
        return generate_latest(), CONTENT_TYPE_LATEST
    return b"# prometheus_client not installed\n", "text/plain"
