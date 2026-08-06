"""
P3-10 gateway evals — router (Arch 10, P3-2).

- strategy scoring: cost / latency / quality / pinned
- tiered routing sends simple queries to the cheap model
- tenant fallback models + provider-family defaults appended deduped
- availability snapshot: cooling-down deployments skipped; all-cooldown
  fallback still returns the best candidate (gateway re-checks at call time)
- latency budget: routing decisions stay far under the 30ms budget under load
"""

import time
from uuid import uuid4

import pytest

from backend.app.gateway.catalog import ModelCatalog, ModelSpec
from backend.app.gateway.router import (
    LatencyTracker,
    Router,
    deployment_key,
    is_valid_strategy,
)
from backend.app.gateway.types import GatewayRequest


def _catalog():
    return ModelCatalog(
        specs=[
            ModelSpec("openai", "gpt-4o", 128_000, 2.5, 10.0),
            ModelSpec("openai", "gpt-4o-mini", 128_000, 0.15, 0.6),
            ModelSpec("anthropic", "claude-sonnet-4", 200_000, 3.0, 15.0),
            ModelSpec("anthropic", "claude-3-5-haiku", 200_000, 0.8, 4.0),
            ModelSpec("google", "gemini-2.5-pro", 1_000_000, 1.25, 10.0),
            ModelSpec("google", "gemini-1.5-flash", 1_000_000, 0.075, 0.3),
        ]
    )


def _request(**kwargs):
    base = dict(
        tenant_id=str(uuid4()),
        messages=[{"role": "user", "content": "hi"}],
        provider="openai",
        model="gpt-4o",
        strategy="cost",
    )
    base.update(kwargs)
    return GatewayRequest(**base)


class TestStrategyScoring:
    def test_cost_prefers_cheapest_candidate(self):
        router = Router(_catalog())
        cands = router.candidates(_request(strategy="cost"))
        assert cands[0] == ("openai", "gpt-4o-mini")

    def test_latency_prefers_fastest_observed(self):
        tracker = LatencyTracker()
        tracker.update("openai", "gpt-4o", 900.0)
        tracker.update("openai", "gpt-4o-mini", 120.0)
        router = Router(_catalog(), latency_tracker=tracker)
        cands = router.candidates(_request(strategy="latency"))
        assert cands[0] == ("openai", "gpt-4o-mini")

    def test_latency_without_observations_keeps_order(self):
        router = Router(_catalog())
        cands = router.candidates(_request(strategy="latency"))
        assert cands[0] == ("openai", "gpt-4o")  # preferred stays first

    def test_quality_keeps_preferred_first(self):
        router = Router(_catalog())
        cands = router.candidates(_request(strategy="quality"))
        assert cands[0] == ("openai", "gpt-4o")

    def test_pinned_returns_exactly_one_deployment(self):
        router = Router(_catalog())
        cands = router.candidates(_request(pinned="anthropic:claude-sonnet-4"))
        assert cands == [("anthropic", "claude-sonnet-4")]

    def test_unknown_strategy_falls_back_to_cost(self):
        router = Router(_catalog())
        cands = router.candidates(_request(strategy="bogus"))
        assert cands[0] == ("openai", "gpt-4o-mini")

    def test_invalid_strategy_detected(self):
        assert is_valid_strategy("cost")
        assert not is_valid_strategy("random")


class TestTiering:
    def test_simple_query_tiered_adds_cheap_model_first(self):
        router = Router(_catalog())
        cands = router.candidates(
            _request(tiered=True, simple_query=True)
        )
        assert cands[0] == ("openai", "gpt-4o-mini")

    def test_complex_query_keeps_capable_model(self):
        router = Router(_catalog())
        cands = router.candidates(
            _request(strategy="quality", tiered=True, simple_query=False)
        )
        assert cands[0] == ("openai", "gpt-4o")

    def test_tenant_cheap_model_override(self):
        router = Router(_catalog())
        cands = router.candidates(
            _request(strategy="cost", tiered=True, simple_query=True),
            {"cheap_model": "google:gemini-1.5-flash"},
        )
        assert cands[0] == ("google", "gemini-1.5-flash")


class TestFallbackCandidates:
    def test_tenant_fallbacks_appended(self):
        router = Router(_catalog())
        cands = router.candidates(
            _request(), {"fallback_models": ["anthropic:claude-sonnet-4"]}
        )
        assert ("anthropic", "claude-sonnet-4") in cands

    def test_tenant_fallback_cross_provider_wins_cost_sort(self):
        router = Router(_catalog())
        cands = router.candidates(
            _request(),
            {"fallback_models": ["google:gemini-1.5-flash"]},
        )
        assert cands[0] == ("google", "gemini-1.5-flash")

    def test_provider_default_fallback_appended(self):
        router = Router(_catalog())
        cands = router.candidates(_request())
        assert ("openai", "gpt-4o-mini") in cands

    def test_deduped(self):
        router = Router(_catalog())
        cands = router.candidates(
            _request(),
            {"fallback_models": ["openai:gpt-4o", "gpt-4o-mini"]},
        )
        assert len(cands) == len(set(cands))


class TestAvailability:
    def test_cooldown_skip_picks_next_available(self):
        router = Router(_catalog())
        request = _request()
        decision = router.decide(
            request,
            lambda p, m: (p, m) == ("openai", "gpt-4o-mini"),
        )
        assert decision.provider == "openai"
        assert decision.model == "gpt-4o-mini"
        assert decision.reason == "available"

    def test_all_cooldown_returns_best_candidate_anyway(self):
        router = Router(_catalog())
        decision = router.decide(_request(), lambda p, m: False)
        assert decision.reason == "all_deployments_cooldown"
        assert decision.provider == "openai"

    def test_latency_budget_under_load(self):
        """Acceptance (P3-10): routing decision cost stays far under the
        30ms budget — the router is pure (no I/O), so this is a regression
        guard against accidental I/O or unbounded candidate growth."""
        router = Router(_catalog())
        request = _request()
        availability = lambda p, m: True  # noqa: E731
        n = 500
        start = time.perf_counter()
        for _ in range(n):
            router.decide(request, availability)
        elapsed_ms = (time.perf_counter() - start) * 1000 / n
        assert elapsed_ms < 1.0  # 30ms budget; we allow 3% of it
        assert router.candidates(request)  # non-empty under load


class TestLatencyTracker:
    def test_ewma_blend(self):
        tracker = LatencyTracker(alpha=0.5)
        tracker.update("openai", "gpt-4o", 100.0)
        tracker.update("openai", "gpt-4o", 200.0)
        assert tracker.get("openai", "gpt-4o") == pytest.approx(150.0)

    def test_unknown_deployment_zero(self):
        assert LatencyTracker().get("openai", "nope") == 0.0

    def test_deployment_key_normalized(self):
        assert deployment_key("OpenAI", "GPT-4o ") == "openai:gpt-4o"
