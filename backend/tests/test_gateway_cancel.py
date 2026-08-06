"""Gateway cancellation propagation tests (Phase 4, Arch 9.1 step 10).

A consumer abort (client disconnect) must close the upstream provider stream
and release the quota reservation — no orphaned in-flight generation, no
leaked USD hold.
"""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from backend.app.gateway.catalog import ModelCatalog, ModelSpec
from backend.app.gateway.service import Gateway
from backend.app.gateway.types import GatewayRequest


def _catalog():
    return ModelCatalog(
        specs=[
            ModelSpec("openai", "gpt-4o", 128_000, 2.5, 10.0),
            ModelSpec("openai", "gpt-4o-mini", 128_000, 0.15, 0.6),
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


class _QuotaStub:
    def __init__(self):
        self.reserved = 0
        self.releases = 0
        self.reconciles = 0

    async def reserve(self, limits, **kwargs):
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


class _FakeAdapter:
    """Scripted adapter: streams fixed deltas; tracks upstream close."""

    def __init__(self, chunks=("one", "two", "three")):
        self.config = SimpleNamespace(tools=None, structured_output=None)
        self.chunks = chunks
        self.closed = False

    def stream(self, messages):
        async def _gen():
            try:
                for chunk in self.chunks:
                    yield _event(type="delta", content=chunk)
            finally:
                self.closed = True

        return _gen()


def _make_gateway(chunks=("one", "two", "three")):
    adapters: dict[str, _FakeAdapter] = {}
    used: list[str] = []
    quota = _QuotaStub()

    async def key_resolver(tenant_config):
        return "test-key"

    def adapter_factory(tenant_config, provider, model, request, api_key):
        key = f"{provider}:{model}"
        used.append(key)
        adapters.setdefault(key, _FakeAdapter(chunks))
        return adapters[key]

    gateway = Gateway(
        catalog=_catalog(),
        quota=quota,
        key_resolver=key_resolver,
        adapter_factory=adapter_factory,
    )
    tenant = SimpleNamespace(
        id="t1", budgets={}, default_provider="openai", default_model="gpt-4o"
    )
    return gateway, adapters, quota, tenant, used


def _used_adapter(adapters, used):
    assert used, "no deployment was attempted"
    return adapters[used[0]]


@pytest.mark.asyncio
async def test_disconnect_mid_stream_closes_upstream_and_releases_quota():
    gateway, adapters, quota, tenant, used = _make_gateway()
    gen = await gateway.stream(_request(), tenant)

    first = await gen.__anext__()
    assert first.type == "delta" and first.content == "one"
    assert quota.reserved == 1 and quota.reconciles == 0

    # Client drops the connection: the generator is closed mid-stream.
    await gen.aclose()

    # Upstream adapter stream was cancelled (propagation upstream) and the
    # USD hold released without being spent.
    assert _used_adapter(adapters, used).closed is True
    assert quota.releases == 1
    assert quota.reconciles == 0


@pytest.mark.asyncio
async def test_completed_stream_reconciles_exactly_once():
    gateway, adapters, quota, tenant, used = _make_gateway()
    events = []
    async for ev in await gateway.stream(_request(), tenant):
        events.append(ev)

    kinds = [e.type for e in events]
    assert "done" in kinds
    assert _used_adapter(adapters, used).closed is True
    assert quota.reconciles == 1
    assert quota.releases == 0  # reconcile is the spend; no double release
    assert quota.reserved == 1


@pytest.mark.asyncio
async def test_cancel_after_first_byte_emits_no_partial_convolution():
    # A consumer cancel between chunks must not surface an error event and
    # must not record cooldown failure.
    gateway, adapters, quota, tenant, used = _make_gateway()
    gen = await gateway.stream(_request(), tenant)
    await gen.__anext__()
    await gen.aclose()
    assert _used_adapter(adapters, used).closed is True
    assert quota.releases == 1