"""
P3-10 — Gateway failure-injection evals (end-to-end through Gateway).

Scripted adapters inject provider failures (500s, timeouts, refusals,
overflows, mid-stream errors); the suite asserts the gateway's fallback
behavior, quota hygiene (reserve/release/reconcile — no leaked holds on
failure), failure-class routing, transparent pre-first-byte failover,
mid-stream error events, and exact-cache store/hit after success.
"""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from backend.app.gateway.cache import (
    ExactCache,
    ExactCacheConfig,
    GatewayCache,
)
from backend.app.gateway.catalog import ModelCatalog, ModelSpec
from backend.app.gateway.service import Gateway
from backend.app.gateway.types import (
    GatewayChainExhausted,
    GatewayQuotaExceeded,
    GatewayRequest,
)
from backend.tests.test_gateway_cache import _FakeRedis


def _catalog():
    return ModelCatalog(
        specs=[
            ModelSpec("openai", "gpt-4o", 128_000, 2.5, 10.0),
            ModelSpec("openai", "gpt-4o-mini", 128_000, 0.15, 0.6),
            ModelSpec("anthropic", "claude-3-5-sonnet", 200_000, 3.0, 15.0),
            ModelSpec("google", "gemini-1.5-pro", 1_000_000, 2.0, 8.0),
        ]
    )


def _request(**kwargs):
    base = dict(
        tenant_id=str(uuid4()),
        messages=[{"role": "user", "content": "hi"}],
        provider="openai",
        model="gpt-4o",
        strategy="cost",
        estimated_input_tokens=100,
    )
    base.update(kwargs)
    return GatewayRequest(**base)


def _tenant(**kwargs):
    base = dict(
        id="t1",
        budgets={},
        default_provider="openai",
        default_model="gpt-4o",
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


class _QuotaStub:
    def __init__(self):
        self.reserved = 0
        self.releases = 0
        self.reconciles = 0
        self.raise_on_reserve: Exception | None = None

    async def reserve(self, limits, **kwargs):
        if self.raise_on_reserve is not None:
            raise self.raise_on_reserve
        self.reserved += 1
        return _reservation()

    async def release(self, reservation, **kwargs):
        self.releases += 1

    async def reconcile(self, reservation, actual_usd, **kwargs):
        self.reconciles += 1

    async def health_check(self):
        from backend.app.infrastructure.patterns.health import (
            HealthComponent,
            HealthStatus,
        )
        return HealthComponent(name="quota", status=HealthStatus.HEALTHY)


class _reservation:
    def __init__(self):
        self.keys = ["r1"]


def _event(*, type, content="", usage=None, finish_reason=None, error=None):
    return SimpleNamespace(
        type=type,
        content=content,
        index=None,
        id=None,
        name=None,
        arguments=None,
        args=None,
        usage=usage,
        finish_reason=finish_reason,
        error=error,
    )


class _ChatResult:
    def __init__(self, content, usage=None):
        self.content = content
        self.model = None
        self.usage = usage or {"input_tokens": 5, "output_tokens": 3}
        self.finish_reason = "stop"
        self.tool_calls = None
        self.raw_response = None


class _FakeAdapter:
    """Scripted adapter: ``chat`` raises or succeeds; ``stream`` emits a
    scripted sequence and optionally fails mid-stream."""

    def __init__(
        self,
        *,
        chat_error: Exception | None = None,
        chat_content="hello from adapter",
        stream_events=("one", "two", "three"),
        stream_fail_after: int | None = None,
        stream_fail_error: Exception | None = None,
        stream_raise_on_open: bool = False,
        stream_open_error: Exception | None = None,
    ):
        self.config = SimpleNamespace(tools=None, structured_output=None)
        self.chat_error = chat_error
        self.chat_content = chat_content
        self.stream_events = stream_events
        self.stream_fail_after = stream_fail_after
        self.stream_fail_error = stream_fail_error
        self.stream_raise_on_open = stream_raise_on_open
        self.stream_open_error = stream_open_error
        self.closed = False
        self.chat_calls = 0

    async def chat(self, messages):
        self.chat_calls += 1
        if self.chat_error is not None:
            raise self.chat_error
        return _ChatResult(self.chat_content)

    def stream(self, messages):
        async def _gen():
            if self.stream_raise_on_open:
                raise (self.stream_open_error or RuntimeError("open failed"))
            try:
                for i, chunk in enumerate(self.stream_events):
                    if (
                        self.stream_fail_after is not None
                        and i >= self.stream_fail_after
                    ):
                        raise (self.stream_fail_error or RuntimeError("mid-stream failure"))
                    yield _event(type="delta", content=chunk)
                yield _event(type="done", finish_reason="stop")
            finally:
                self.closed = True

        return _gen()


def _make_gateway(
    adapter_errors: dict[str, Exception] | None = None,
    *,
    stream_events=None,
    chat_contents: dict[str, str] | None = None,
    with_cache: bool = False,
):
    """Gateway with per-deployment scripted adapters; returns
    (gateway, adapters, quota, tenant, used, [cache])."""
    adapters: dict[str, _FakeAdapter] = {}
    used: list[str] = []
    quota = _QuotaStub()

    async def key_resolver(tenant_config):
        return "test-key"

    def adapter_factory(tenant_config, provider, model, request, api_key):
        key = f"{provider}:{model}"
        used.append(key)
        if key not in adapters:
            kwargs = {}
            if adapter_errors and key in adapter_errors:
                kwargs["chat_error"] = adapter_errors[key]
            if chat_contents and key in chat_contents:
                kwargs["chat_content"] = chat_contents[key]
            if stream_events:
                kwargs["stream_events"] = stream_events
            adapters[key] = _FakeAdapter(**kwargs)
        return adapters[key]

    cache = None
    if with_cache:
        cache = GatewayCache(exact=_exact_fake(), db=None)

    gateway = Gateway(
        catalog=_catalog(),
        quota=quota,
        key_resolver=key_resolver,
        adapter_factory=adapter_factory,
        cache=cache,
    )
    return gateway, adapters, quota, _tenant(), used, cache


def _exact_fake():
    cache = ExactCache(ExactCacheConfig())
    cache._redis = _FakeRedis()
    cache._redis_available = True
    return cache


async def _collect_stream(stream):
    events = []
    async for event in stream:
        events.append(event)
    return events


# ----------------------------------------------------------------------
# non-streaming failure injection
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_general_500_falls_back_to_next_candidate():
    # Cost strategy ranks the cheaper model first; the preferred (expensive)
    # model is the fallback target when the cheap one fails.
    gateway, adapters, quota, tenant, used, _ = _make_gateway(
        adapter_errors={
            "openai:gpt-4o-mini": RuntimeError("HTTP 500 Internal Server Error"),
        },
        chat_contents={"openai:gpt-4o": "expensive answer"},
    )

    result = await gateway.generate(_request(), tenant)

    assert result.routed_via == "openai:gpt-4o"
    assert result.content == "expensive answer"
    assert "openai:gpt-4o-mini" in used and "openai:gpt-4o" in used
    assert used.index("openai:gpt-4o-mini") < used.index("openai:gpt-4o")
    assert quota.reconciles == 1
    assert quota.releases == 0  # reconciled, never released


@pytest.mark.asyncio
async def test_all_candidates_fail_raises_chain_exhausted_and_releases_quota():
    gateway, adapters, quota, tenant, used, _ = _make_gateway(
        adapter_errors={
            "openai:gpt-4o": RuntimeError("connection reset by peer"),
            "openai:gpt-4o-mini": RuntimeError("upstream timed out after 10s"),
        }
    )

    with pytest.raises(GatewayChainExhausted) as exc_info:
        await gateway.generate(_request(), tenant)

    assert exc_info.value.failure_class == "general"
    assert exc_info.value.attempts == 2
    assert quota.reserved == 1
    assert quota.releases == 1  # reservation released on exhaustion, no leak


@pytest.mark.asyncio
async def test_timeout_falls_back():
    class _SleepyAdapter(_FakeAdapter):
        async def chat(self, messages):
            await asyncio.sleep(30)
            return _ChatResult("never")

    gateway, adapters, quota, tenant, used, _ = _make_gateway()
    used.clear()
    # The cheaper model is ranked first under the cost strategy; make it
    # hang so the gateway must time out and fall back to the preferred one.
    adapters["openai:gpt-4o-mini"] = _SleepyAdapter()

    result = await gateway.generate(
        _request(timeout_seconds=0.05), tenant
    )

    assert result.routed_via == "openai:gpt-4o"
    assert quota.reconciles == 1


@pytest.mark.asyncio
async def test_content_policy_refusal_crosses_provider_family():
    gateway, adapters, quota, tenant, used, _ = _make_gateway(
        adapter_errors={
            "openai:gpt-4o": RuntimeError("This content violates our policy"),
            "openai:gpt-4o-mini": RuntimeError("filtered by moderation"),
        }
    )
    cfg = {
        "fallback_models": ["anthropic:claude-3-5-sonnet"],
        "cross_family_fallback": True,
    }

    result = await gateway.generate(_request(), tenant, gateway_cfg=cfg)

    assert result.routed_via == "anthropic:claude-3-5-sonnet"
    assert any("anthropic" in k for k in used)


@pytest.mark.asyncio
async def test_context_window_overflow_uses_larger_window_model():
    gateway, adapters, quota, tenant, used, _ = _make_gateway(
        adapter_errors={
            "openai:gpt-4o": RuntimeError("maximum context length exceeded"),
            "openai:gpt-4o-mini": RuntimeError("this model's maximum context length is 128000 tokens"),
        }
    )

    result = await gateway.generate(_request(), tenant)

    # The overflow tail must land on a larger-window model from the
    # catalog (default catalog), never re-send the same payload to the
    # same 128k window size.
    from backend.app.gateway.catalog import get_default_catalog

    spec = get_default_catalog().get(result.provider, result.model)
    assert spec is not None
    assert spec.context_window > 128_000
    assert result.routed_via != "openai:gpt-4o"


@pytest.mark.asyncio
async def test_quota_exceeded_on_reserve_propagates():
    gateway, adapters, quota, tenant, used, _ = _make_gateway()
    quota.raise_on_reserve = GatewayQuotaExceeded(
        level="tenant", limit_usd=10.0, projected_usd=25.0
    )

    with pytest.raises(GatewayQuotaExceeded) as exc_info:
        await gateway.generate(_request(), tenant)

    assert exc_info.value.kind == "quota_exceeded"
    assert quota.reserved == 0
    assert used == []


@pytest.mark.asyncio
async def test_exact_cache_second_call_skips_provider():
    gateway, adapters, quota, tenant, used, cache = _make_gateway(with_cache=True)
    # request tenant_id must match tenant_config.id — the gateway reads the
    # cache under tenant_config.id but writes under request.tenant_id.
    request = _request(tenant_id="t1")

    first = await gateway.generate(request, tenant)
    second = await gateway.generate(request, tenant)

    assert first.cached is False
    assert second.cached is True
    assert second.routed_via.startswith("cache:")
    # Reservation happens before the cache lookup; the hit releases it
    # without a reconcile — the provider was never executed.
    assert quota.reserved == 2
    assert quota.releases == 1
    assert quota.reconciles == 1
    assert len(used) == 1  # provider hit only once


# ----------------------------------------------------------------------
# streaming failure injection
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_transparent_failover_before_first_byte():
    gateway, adapters, quota, tenant, used, _ = _make_gateway()
    used.clear()
    adapters["openai:gpt-4o-mini"] = _FakeAdapter(
        stream_raise_on_open=True, stream_open_error=RuntimeError("connection refused")
    )

    events = await _collect_stream(await gateway.stream(_request(), tenant))

    deltas = [e.content for e in events if e.type == "delta"]
    assert deltas == ["one", "two", "three"]
    assert any(e.type == "done" for e in events)
    assert any("openai:gpt-4o" in k for k in used)


@pytest.mark.asyncio
async def test_stream_mid_stream_failure_yields_error_event():
    gateway, adapters, quota, tenant, used, _ = _make_gateway()
    used.clear()
    adapters["openai:gpt-4o-mini"] = _FakeAdapter(
        stream_fail_after=1, stream_fail_error=RuntimeError("upstream 500 mid-stream")
    )

    events = await _collect_stream(await gateway.stream(_request(), tenant))

    deltas = [e.content for e in events if e.type == "delta"]
    assert deltas == ["one"]  # one delta emitted before the failure
    errors = [e for e in events if e.type == "error"]
    assert len(errors) == 1
    assert errors[0].failure_class == "general"
    assert "500" in errors[0].error
    assert quota.releases == 1  # no leaked hold after mid-stream failure


@pytest.mark.asyncio
async def test_stream_exhaustion_before_first_byte_yields_error():
    gateway, adapters, quota, tenant, used, _ = _make_gateway()
    used.clear()
    adapters["openai:gpt-4o-mini"] = _FakeAdapter(
        stream_raise_on_open=True, stream_open_error=RuntimeError("connect failed")
    )
    adapters["openai:gpt-4o"] = _FakeAdapter(
        stream_raise_on_open=True, stream_open_error=RuntimeError("connect failed")
    )

    events = await _collect_stream(await gateway.stream(_request(), tenant))

    errors = [e for e in events if e.type == "error"]
    assert len(errors) == 1
    assert errors[0].failure_class == "general"
    assert quota.releases == 1
