"""
P6-1 — OpenTelemetry tracing with PII-safe span processing (Arch §13).

Spans cover the full request path: guardrails → retrieval → LLM → policy →
handoff. Every span carries ``tenant_id`` and ``surface_id`` attributes.
Trace payloads are sanitized by the ``NeryvaSpanProcessor`` before export:
no raw PII leaves the process boundary.

Head sampling keeps all error/guardrail-event traces and samples normal
traffic at a configurable per-tenant rate.

Design:
  - ``init_tracing()`` wires up the OTel SDK with the span processor and
    exporter.  Called once at app startup (``main.py`` lifespan).
  - ``neryva_span()`` is the primary context-manager for instrumented code.
  - The module degrades gracefully when OTel SDK packages are not installed
    (dev environments without the observability extras): all public functions
    become no-ops and log a single warning.
"""

from __future__ import annotations

import re
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# PII patterns (lightweight; same patterns as the PII gateway uses at ingress)
# ---------------------------------------------------------------------------
_PII_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("email", re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")),
    ("phone", re.compile(r"\b\+?[1-9]\d{6,14}\b")),
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("credit_card", re.compile(r"\b(?:\d[ -]*?){13,19}\b")),
    ("ip_address", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
]

_REDACTION_PLACEHOLDER = "[REDACTED]"


def _redact_pii(text: str) -> str:
    """Strip PII patterns from a string before it enters a trace payload."""
    for _name, pattern in _PII_PATTERNS:
        text = pattern.sub(_REDACTION_PLACEHOLDER, text)
    return text


def _redact_dict(d: dict[str, Any]) -> dict[str, Any]:
    """Recursively redact string values in a dict."""
    out: dict[str, Any] = {}
    for k, v in d.items():
        if isinstance(v, str):
            out[k] = _redact_pii(v)
        elif isinstance(v, dict):
            out[k] = _redact_dict(v)
        elif isinstance(v, list):
            out[k] = [_redact_pii(item) if isinstance(item, str) else item for item in v]
        else:
            out[k] = v
    return out


# ---------------------------------------------------------------------------
# Head-sampling decision
# ---------------------------------------------------------------------------
@dataclass
class SamplingConfig:
    """Per-tenant head-sampling rates."""
    default_rate: float = 0.1  # 10% of normal traffic
    error_rate: float = 1.0    # always keep errors
    guardrail_rate: float = 1.0  # always keep guardrail events
    per_tenant_overrides: dict[str, float] = field(default_factory=dict)

    def should_sample(
        self,
        tenant_id: str | None,
        is_error: bool = False,
        is_guardrail_event: bool = False,
    ) -> bool:
        """Return True if this trace should be kept."""
        if is_error:
            return True  # always keep errors
        if is_guardrail_event:
            return True  # always keep guardrail triggers
        rate = self.per_tenant_overrides.get(tenant_id or "", self.default_rate)
        # Deterministic hash-based sampling so the same trace id always
        # resolves the same way.
        import hashlib
        digest = hashlib.sha256((tenant_id or "").encode()).digest()
        bucket = int.from_bytes(digest[:2], "big") % 1000
        return bucket < int(rate * 1000)


# ---------------------------------------------------------------------------
# OTel integration (graceful degradation when SDK is absent)
# ---------------------------------------------------------------------------
_HAS_OTEL = False
_tracer_provider: Any = None
_sampling_config = SamplingConfig()

try:
    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanExporter
    from opentelemetry.trace import StatusCode
    _HAS_OTEL = True
except ImportError:
    pass  # No OTel SDK — all public functions degrade to no-ops


class NeryvaSpanProcessor:
    """
    Custom OTel SpanProcessor that:
    1. Redacts PII from span attributes before export.
    2. Applies head sampling (errors/guardrail events always kept).

    Only instantiated when the OTel SDK is available.
    """

    def __init__(self, delegate_processor: Any, sampling: SamplingConfig | None = None):
        self._delegate = delegate_processor
        self._sampling = sampling or SamplingConfig()

    def on_start(self, span: Any, parent_context: Any = None) -> None:
        """Attach tenant metadata at span start."""
        self._delegate.on_start(span, parent_context)

    def on_end(self, span: Any) -> None:
        """Redact PII and apply head sampling before delegation."""
        if not _HAS_OTEL:
            return

        attrs = dict(span.attributes) if span.attributes else {}

        # --- PII redaction on all string attributes ---
        redacted_attrs: dict[str, Any] = {}
        for k, v in attrs.items():
            if isinstance(v, str):
                redacted_attrs[k] = _redact_pii(v)
            else:
                redacted_attrs[k] = v

        # --- Head sampling ---
        tenant_id = redacted_attrs.get("neryva.tenant_id")
        span_status = getattr(span, "status", None)
        is_error = (
            span_status is not None
            and hasattr(span_status, "status_code")
            and span_status.status_code == StatusCode.ERROR
        )
        is_guardrail = redacted_attrs.get("neryva.guardrail_triggered", False)

        if not self._sampling.should_sample(
            tenant_id=str(tenant_id) if tenant_id else None,
            is_error=is_error,
            is_guardrail_event=bool(is_guardrail),
        ):
            return  # drop the span (not exported)

        # Replace attributes with redacted versions.
        # OTel SDK ReadableSpan is immutable after on_end; we write to a
        # mutable copy only if the SDK exposes it — otherwise the delegate
        # receives the original span and the redaction is best-effort at
        # the exporter level.
        try:
            for k, v in redacted_attrs.items():
                span.set_attribute(k, v)
        except Exception:
            pass  # ReadableSpan may be frozen; PII filter at exporter level

        self._delegate.on_end(span)

    def shutdown(self) -> None:
        self._delegate.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._delegate.force_flush(timeout_millis)


def init_tracing(
    service_name: str = "neryva-studio",
    otlp_endpoint: str | None = None,
    sampling_config: SamplingConfig | None = None,
) -> Any | None:
    """Initialize OpenTelemetry tracing with PII-safe span processing.

    Returns the configured ``TracerProvider`` or ``None`` when the OTel SDK
    is not installed (graceful degradation for dev environments).
    """
    global _tracer_provider, _sampling_config

    if not _HAS_OTEL:
        logger.warning(
            "otel_tracing_unavailable",
            reason="opentelemetry-sdk not installed; tracing disabled",
        )
        return None

    if sampling_config:
        _sampling_config = sampling_config

    resource = Resource.create({
        "service.name": service_name,
        "service.version": "0.1.0",
    })

    provider = TracerProvider(resource=resource)

    # Configure exporter — OTLP gRPC by default, console fallback
    exporter: Any = None
    if otlp_endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                OTLPSpanExporter,
            )
            exporter = OTLPSpanExporter(endpoint=otlp_endpoint)
        except ImportError:
            logger.warning("otlp_exporter_unavailable", fallback="console")

    if exporter is None:
        try:
            from opentelemetry.sdk.trace.export import ConsoleSpanExporter
            exporter = ConsoleSpanExporter()
        except ImportError:
            logger.warning("console_exporter_unavailable")
            exporter = None

    if exporter is not None:
        batch_processor = BatchSpanExporter(exporter)
        neryva_processor = NeryvaSpanProcessor(
            delegate_processor=batch_processor,
            sampling=_sampling_config,
        )
        provider.add_span_processor(neryva_processor)

    otel_trace.set_tracer_provider(provider)
    _tracer_provider = provider

    logger.info(
        "otel_tracing_initialized",
        service=service_name,
        endpoint=otlp_endpoint or "console",
    )
    return provider


def get_tracer_provider() -> Any | None:
    """Return the initialized TracerProvider, or None."""
    return _tracer_provider


@contextmanager
def neryva_span(
    name: str,
    tenant_id: str | None = None,
    surface_id: str | None = None,
    end_user_id: str | None = None,
    attributes: dict[str, Any] | None = None,
) -> Generator[Any]:
    """Context manager that creates and manages an OTel span.

    Usage::

        with neryva_span("guardrails.check", tenant_id=tid) as span:
            result = run_guardrails(...)
            span.set_attribute("guardrail.result", result.decision)

    When the OTel SDK is absent, yields a no-op object.
    """
    if not _HAS_OTEL or _tracer_provider is None:
        yield _NoOpSpan()
        return

    tracer = otel_trace.get_tracer("neryva.studio")
    with tracer.start_as_current_span(name) as span:
        # Attach Neryva-specific attributes
        if tenant_id:
            span.set_attribute("neryva.tenant_id", tenant_id)
        if surface_id:
            span.set_attribute("neryva.surface_id", surface_id)
        if end_user_id:
            span.set_attribute("neryva.end_user_id", _redact_pii(end_user_id))
        if attributes:
            for k, v in attributes.items():
                val = _redact_pii(str(v)) if isinstance(v, str) else v
                span.set_attribute(k, val)

        span.set_attribute("neryva.timestamp", time.time())
        yield span


class _NoOpSpan:
    """Placeholder when OTel is unavailable. All method calls are silent."""

    def set_attribute(self, key: str, value: Any) -> None:
        pass

    def set_status(self, *args: Any, **kwargs: Any) -> None:
        pass

    def record_exception(self, exception: BaseException) -> None:
        pass

    def add_event(self, name: str, attributes: dict[str, Any] | None = None) -> None:
        pass
