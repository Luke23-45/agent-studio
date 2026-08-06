"""
P3-10 gateway evals — fallback chains (Arch 10, P3-3).

- failure classification: general (500/timeout/connection) vs
  content_policy (refusals) vs context_window (overflow markers)
- chain targets per class: candidates first, then cross-family for
  content policy, larger-window models for overflows
- ordering + dedup; cross-family fallback flag gates the content-policy tail
- overflow detection drives the orchestration compaction hook
"""

from uuid import uuid4

import pytest

from backend.app.gateway.catalog import get_default_catalog
from backend.app.gateway.fallback import (
    FALLBACK_CONTENT_POLICY,
    FALLBACK_CONTEXT_WINDOW,
    FALLBACK_GENERAL,
    FallbackChain,
    build_fallback_chain,
    classify_failure,
    is_overflow_failure,
)
from backend.app.gateway.types import GatewayRequest


def _request(provider="openai", **kwargs):
    base = dict(
        tenant_id=str(uuid4()),
        messages=[{"role": "user", "content": "hi"}],
        provider=provider,
        model="gpt-4o",
        strategy="cost",
    )
    base.update(kwargs)
    return GatewayRequest(**base)


class TestClassifyFailure:
    @pytest.mark.parametrize(
        "message",
        [
            "connection reset by peer",
            "HTTP 500 Internal Server Error",
            "upstream timed out after 10s",
            "Service Unavailable",
        ],
    )
    def test_general_markers(self, message):
        assert classify_failure(RuntimeError(message)) == FALLBACK_GENERAL

    @pytest.mark.parametrize(
        "message",
        [
            "content policy violation",
            "This content violates our policy",
            "safety system refused the request",
            "output blocked: refusal",
            "filtered by moderation",
        ],
    )
    def test_content_policy_markers(self, message):
        assert classify_failure(RuntimeError(message)) == FALLBACK_CONTENT_POLICY

    @pytest.mark.parametrize(
        "message",
        [
            "maximum context length exceeded",
            "This model's maximum context length is 128000 tokens",
            "prompt is too long, please reduce",
            "context_length_exceeded",
            "token limit exceeded",
        ],
    )
    def test_context_window_markers(self, message):
        assert classify_failure(RuntimeError(message)) == FALLBACK_CONTEXT_WINDOW

    def test_overflow_detection(self):
        assert is_overflow_failure(RuntimeError("maximum context length"))
        assert not is_overflow_failure(RuntimeError("refused by safety system"))


class TestChainTargets:
    def _chain(self, request=None, cfg=None, cross=True):
        request = request or _request()
        candidates = [("openai", "gpt-4o"), ("openai", "gpt-4o-mini")]
        return FallbackChain(
            request,
            candidates,
            cfg or {},
            family_cross_fallback=cross,
        )

    def test_general_keeps_candidate_order_deduped(self):
        chain = self._chain()
        targets = chain.targets(FALLBACK_GENERAL)
        assert targets == [("openai", "gpt-4o"), ("openai", "gpt-4o-mini")]
        assert len(targets) == len(set(targets))

    def test_content_policy_appends_cross_family(self):
        chain = self._chain(
            cfg={"fallback_models": ["anthropic:claude-3-5-haiku"]}
        )
        targets = chain.targets(FALLBACK_CONTENT_POLICY)
        assert ("openai", "gpt-4o") in targets
        assert ("anthropic", "claude-3-5-haiku") in targets

    def test_cross_family_flag_disables_tail(self):
        chain = self._chain(
            cfg={"fallback_models": ["anthropic:claude-3-5-haiku"]},
            cross=False,
        )
        targets = chain.targets(FALLBACK_CONTENT_POLICY)
        assert all(p == "openai" for p, _ in targets)

    def test_content_policy_without_family_entries_retries_candidates(self):
        chain = self._chain()
        targets = chain.targets(FALLBACK_CONTENT_POLICY)
        assert targets == [("openai", "gpt-4o"), ("openai", "gpt-4o-mini")]

    def test_tenant_fallback_entry_used_for_family(self):
        chain = self._chain(
            cfg={"fallback_models": ["anthropic:claude-3-5-haiku"]}
        )
        targets = chain.targets(FALLBACK_CONTENT_POLICY)
        assert ("anthropic", "claude-3-5-haiku") in targets

    def test_context_window_appends_larger_window_models(self):
        chain = self._chain()
        targets = chain.targets(FALLBACK_CONTEXT_WINDOW)
        assert len(targets) >= 2  # candidates + 2 larger-window models
        larger = targets[2:]
        assert len(larger) == 2
        # Both larger-window models are distinct from the originals.
        assert all(t not in [("openai", "gpt-4o"), ("openai", "gpt-4o-mini")] for t in larger)

    def test_large_window_picks_top_window_models(self):
        chain = self._chain()
        larger = chain.targets(FALLBACK_CONTEXT_WINDOW)[2:]
        windows = [
            get_default_catalog().context_window(p, m)
            for p, m in larger
        ]
        # Top of the default catalog is the 1M class; the two overflow
        # targets never repeat the failed window size.
        assert len(larger) == 2
        assert all(w >= 200_000 for w in windows)
        assert max(windows) == 1_000_000
        assert all(
            get_default_catalog().context_window(*t)
            >= get_default_catalog().context_window("openai", "gpt-4o")
            for t in larger
        )

    def test_builder_wires_cfg_flag(self):
        chain = build_fallback_chain(_request(), [("openai", "gpt-4o")], {})
        assert chain.family_cross_fallback is True
        chain = build_fallback_chain(
            _request(), [("openai", "gpt-4o")], {"cross_family_fallback": False}
        )
        assert chain.family_cross_fallback is False
