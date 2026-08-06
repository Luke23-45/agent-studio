"""
Gateway contract types (Arch 10, P3-1/P3-9).

The gateway is the only component that talks to providers. These types are
the interface boundary: orchestration and routes construct
``GatewayRequest`` and consume ``GatewayResult`` / ``GatewayStreamEvent`` —
they never see provider objects, wire formats, or keys. Error types carry
a stable ``kind`` so callers map them to HTTP statuses without importing
provider SDKs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator


class GatewayError(Exception):
    """Base class for gateway-level failures (never provider-specific)."""

    kind: str = "gateway_error"

    def __init__(self, message: str, *, kind: str | None = None):
        self.message = message
        if kind is not None:
            self.kind = kind
        super().__init__(message)


class GatewayAdmissionError(GatewayError):
    """Concurrent-generation cap exceeded; caller returns 429 + Retry-After."""

    kind = "admission_exceeded"

    def __init__(self, retry_after: float):
        self.retry_after = retry_after
        super().__init__(f"admission_limit_exceeded retry_after={retry_after:.0f}s")


class GatewayQuotaExceeded(GatewayError):
    """USD quota exhausted at one of the four budget levels (Arch 6.3.8)."""

    kind = "quota_exceeded"

    def __init__(self, level: str, limit_usd: float, projected_usd: float):
        self.level = level
        self.limit_usd = limit_usd
        self.projected_usd = projected_usd
        super().__init__(
            f"quota_exceeded level={level} limit_usd={limit_usd:.2f} "
            f"projected_usd={projected_usd:.2f}"
        )


class GatewayConfigurationError(GatewayError):
    """No usable deployment could be configured (key missing, provider unknown)."""

    kind = "gateway_configuration"

    def __init__(self, message: str, *, provider: str | None = None):
        self.provider = provider
        super().__init__(message)


class GatewayChainExhausted(GatewayError):
    """Every target in the fallback chain failed for the same failure class.

    ``kind`` is the failure class that ultimately exhausted the chain
    (general | content_policy | context_window); the turn must degrade per
    tenant configuration — never fail open.
    """

    kind = "chain_exhausted"

    def __init__(
        self,
        provider: str,
        model: str,
        reason: str,
        *,
        failure_class: str = "general",
        attempts: int = 0,
    ):
        self.provider = provider
        self.model = model
        self.failure_class = failure_class
        self.attempts = attempts
        super().__init__(
            f"llm_chain_exhausted provider={provider} model={model} "
            f"class={failure_class} attempts={attempts}: {reason}"
        )


@dataclass(frozen=True)
class GatewayRequest:
    """One model call request, fully provider-agnostic.

    ``provider``/``model`` are *preferences* (from tenant config or surface
    pin): the router may pick a different deployment (tiering, fallback).
    ``strategy`` is the per-tenant routing strategy (cost/latency/quality/
    pinned). Tool schemas are OpenAI-function format; the adapters
    translate per provider. ``structured_output`` is a JSON schema dict
    (P3-1) when the caller wants a schema-validated object back.
    """

    tenant_id: str
    messages: list[dict[str, Any]]
    request_id: str = ""
    provider: str = "openai"
    model: str | None = None
    strategy: str = "cost"
    temperature: float = 0.7
    max_tokens: int | None = None
    tools: list[dict[str, Any]] = field(default_factory=list)
    structured_output: dict[str, Any] | None = None
    surface_id: str | None = None
    end_user_id: str | None = None
    conversation_id: str | None = None
    session_id: str | None = None
    tiered: bool = False
    simple_query: bool = False
    estimated_input_tokens: int | None = None
    timeout_seconds: float | None = None
    # Optional per-request routing override: force exactly this deployment
    # (surface model pin / operator test) — no tiering, no fallback.
    pinned: str | None = None


@dataclass(frozen=True)
class RouteDecision:
    """The router's pick for this request."""

    provider: str
    model: str
    strategy: str
    candidates_considered: int = 1
    reason: str = ""


@dataclass(frozen=True)
class GatewayResult:
    """Non-streaming result. ``usage`` uses the canonical keys from
    ``adapters.llm.provider`` (input/output/reasoning/cached tokens).
    ``tool_calls`` are canonical ``{"id", "name", "arguments"}`` dicts.
    ``routed_via`` names the provider/model that answered; ``attempts`` is
    the number of deployments tried before success; ``cached`` marks an
    exact/semantic cache hit.
    """

    content: str
    model: str
    provider: str
    usage: dict[str, int] = field(default_factory=dict)
    finish_reason: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    routed_via: str = ""
    attempts: int = 1
    cached: bool = False
    latency_ms: float = 0.0
    raw_response: Any = None


@dataclass(frozen=True)
class GatewayStreamEvent:
    """Normalized streaming event (P3-1). ``type`` is one of:

    - ``delta``            : token text (``content``)
    - ``tool_use_start``   : tool call opened (``index``, ``id``, ``name``)
    - ``tool_use_delta``   : tool args fragment (``index``, ``args``)
    - ``tool_use_end``     : tool call complete (``index``, ``id``, ``name``,
                             ``arguments`` decoded dict; None when malformed)
    - ``usage``            : canonical usage accumulator (``usage`` dict)
    - ``done``             : stream complete (``finish_reason``)
    - ``error``            : stream failed (``error`` string, ``failure_class``)
    """

    type: str
    content: str = ""
    index: int | None = None
    id: str | None = None
    name: str | None = None
    args: str | None = None
    arguments: dict[str, Any] | None = None
    usage: dict[str, int] | None = None
    finish_reason: str | None = None
    error: str | None = None
    failure_class: str | None = None
    provider: str | None = None
    model: str | None = None


GatewayStream = AsyncIterator[GatewayStreamEvent]
