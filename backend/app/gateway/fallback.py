"""
Fallback chains (Arch 10, P3-3).

Three classes, each with its own ordered target list:

- ``general``         : timeout / 5xx / connection — try the next healthy
                        deployment, then the tenant-configured fallback
                        models, then the provider-family default.
- ``content_policy``  : provider refused the request (Anthropic output
                        block, OpenAI safety refusal) — retrying the same
                        provider rarely helps; move to another provider
                        family when the tenant allows it.
- ``context_window``  : request exceeded the model's context window — move
                        to a larger-window model, never re-send the same
                        overflow payload to the same window size.

Failover is transparent only before the first byte; a mid-stream failure
surfaces to the connection tier (the gateway yields an ``error`` event and
the caller decides how to degrade).
"""

from __future__ import annotations

from typing import Iterable

from backend.app.gateway.router import tenant_fallback_candidates
from backend.app.gateway.types import GatewayRequest

FALLBACK_GENERAL = "general"
FALLBACK_CONTENT_POLICY = "content_policy"
FALLBACK_CONTEXT_WINDOW = "context_window"

_ALL_CLASSES = (FALLBACK_GENERAL, FALLBACK_CONTENT_POLICY, FALLBACK_CONTEXT_WINDOW)

# Provider error signatures that identify each failure class. Matching is
# case-insensitive substring over the exception message (provider-agnostic:
# the adapters surface SDK errors verbatim).
_CONTENT_POLICY_MARKERS = (
    "content policy",
    "content_policy",
    "safety system",
    "refused",
    "refusal",
    "output blocked",
    "output_blocked",
    "inappropriate content",
    "filtered",
    "blocked by policy",
    "violates our policy",
    "responses_to_same",
    "moderation",
    "policy violation",
)

_CONTEXT_WINDOW_MARKERS = (
    "maximum context length",
    "context length exceeded",
    "context window",
    "prompt is too long",
    "input is too long",
    "context_length_exceeded",
    "maximum tokens",
    "token limit exceeded",
    "too many tokens",
)

# Provider families we may fall across for content-policy refusals.
_FAMILY_CROSS_FALLBACK = {
    "openai": ("anthropic", "google"),
    "anthropic": ("openai", "google"),
    "google": ("openai", "anthropic"),
    "azure": ("openai",),
    "custom": ("openai",),
}


def classify_failure(exc: Exception) -> str:
    """Map an exception to a fallback chain class (default: general)."""
    text = str(exc).lower()
    if any(m in text for m in _CONTEXT_WINDOW_MARKERS):
        return FALLBACK_CONTEXT_WINDOW
    if any(m in text for m in _CONTENT_POLICY_MARKERS):
        return FALLBACK_CONTENT_POLICY
    return FALLBACK_GENERAL


def is_overflow_failure(exc: Exception) -> bool:
    """True when the failure is a context-window overflow (compaction hook
    uses this to trigger its one-shot recovery before the chain moves on)."""
    return classify_failure(exc) == FALLBACK_CONTEXT_WINDOW


class FallbackChain:
    """Ordered deployment lists per failure class for one request."""

    def __init__(
        self,
        request: GatewayRequest,
        candidates: list[tuple[str, str]],
        gateway_cfg: dict | None = None,
        *,
        family_cross_fallback: bool = True,
    ):
        self.request = request
        self.candidates = candidates
        self.gateway_cfg = gateway_cfg or {}
        self.family_cross_fallback = family_cross_fallback

    def targets(self, failure_class: str) -> list[tuple[str, str]]:
        """Deployments to try for a failure class, in order, deduped.

        Always starts from the router's candidate list (the preferred
        deployment is retried across general failures after a cooldown
        reset). Class-specific tails extend the list.
        """
        ordered: list[tuple[str, str]] = []
        for candidate in self.candidates:
            if candidate not in ordered:
                ordered.append(candidate)
        if failure_class == FALLBACK_CONTENT_POLICY and self.family_cross_fallback:
            ordered.extend(self._cross_family())
        if failure_class == FALLBACK_CONTEXT_WINDOW:
            ordered.extend(self._large_window())
        return ordered

    def _cross_family(self) -> list[tuple[str, str]]:
        """Other provider families for content-policy refusals."""
        families = _FAMILY_CROSS_FALLBACK.get(self.request.provider, ())
        result: list[tuple[str, str]] = []
        for provider in families:
            for candidate in self.candidates:
                if candidate[0] == provider and candidate not in result:
                    result.append(candidate)
            if not result or not any(c[0] == provider for c in result):
                # Provider family present only via tenant fallback entries.
                for entry in tenant_fallback_candidates(self.gateway_cfg):
                    if ":" in entry and entry.split(":", 1)[0].strip().lower() == provider:
                        provider_, model = entry.split(":", 1)
                        candidate = (provider_.strip().lower(), model.strip())
                        if candidate not in result:
                            result.append(candidate)
        return result

    def _large_window(self) -> list[tuple[str, str]]:
        """Larger-context models for context-window overflows.

        Prefers the two largest-window models from the catalog that are
        not already in the candidate list — retrying an overflow with the
        same window size is pointless.
        """
        from backend.app.gateway.catalog import get_default_catalog

        specs = sorted(
            get_default_catalog().list_all(),
            key=lambda s: s.context_window,
            reverse=True,
        )
        result: list[tuple[str, str]] = []
        for spec in specs:
            candidate = (spec.provider, spec.model)
            if candidate in self.candidates or candidate in result:
                continue
            result.append(candidate)
            if len(result) >= 2:
                break
        return result


def build_fallback_chain(
    request: GatewayRequest,
    candidates: Iterable[tuple[str, str]],
    gateway_cfg: dict | None = None,
    *,
    family_cross_fallback: bool | None = None,
) -> FallbackChain:
    if family_cross_fallback is None:
        family_cross_fallback = bool(
            (gateway_cfg or {}).get("cross_family_fallback", True)
        )
    return FallbackChain(
        request,
        list(candidates),
        gateway_cfg,
        family_cross_fallback=family_cross_fallback,
    )
