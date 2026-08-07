"""
Observability infrastructure (Phase 6, Arch §13).

Provides OpenTelemetry tracing with PII-safe span processors, Prometheus
metrics for SLOs/dashboards, and the head-sampling policy for error/guardrail
events.

Existing Langfuse adapter in ``adapters/tracing/`` is preserved and continues
to serve its purpose (generation-level tracing to the Langfuse platform).
This module adds the complementary OTel/Prometheus layer the architecture
requires for infrastructure-level observability.
"""

from .metrics import NeryvaMetrics, get_metrics, init_metrics
from .tracing import (
    NeryvaSpanProcessor,
    get_tracer_provider,
    init_tracing,
    neryva_span,
)

__all__ = [
    "NeryvaMetrics",
    "NeryvaSpanProcessor",
    "get_metrics",
    "get_tracer_provider",
    "init_metrics",
    "init_tracing",
    "neryva_span",
]
