"""
Gateway service (Arch 10, P3-9).

The gateway is the ONLY component that talks to providers. Orchestration
and routes construct ``GatewayRequest`` and consume ``GatewayResult`` /
``GatewayStreamEvent`` — they never see provider SDKs, wire formats, or
keys (ledger P3-9 acceptance: zero provider branches in application code).

Request path (``generate`` / ``stream``):
    candidates -> availability snapshot (Redis cooldowns) -> route ->
    quota reserve -> exact-cache check -> execute with fallback chain per
    failure class -> cooldown accounting -> quota reconcile -> cost ledger
    -> cache store.

Admission (P0-7) precedes the gateway in the route. Key handling is
confined to ``key_resolver`` (default: ``ProviderKeyService``); adapter
construction is confined to ``adapter_factory`` (default:
``create_llm_adapter``). The router stays pure and synchronous: Redis
availability is snapshotted once per request (bounded GETs) and passed to
it as a plain dict. Everything Redis-backed degrades visibly (DEGRADED
health, logged) — never silently, per codebase convention.
"""

from __future__ import annotations

import asyncio
import json
import time
import structlog
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable
from uuid import uuid4

from backend.app.gateway.cache import CachedEntry, GatewayCache
from backend.app.gateway.catalog import ModelCatalog, get_default_catalog
from backend.app.gateway.cooldown import RedisCooldownCache
from backend.app.gateway.fallback import build_fallback_chain, classify_failure
from backend.app.gateway.ledger import CostLedger, UsageRecord
from backend.app.gateway.quota import QuotaLimits, QuotaService
from backend.app.gateway.router import Router
from backend.app.gateway.types import (
    GatewayChainExhausted,
    GatewayConfigurationError,
    GatewayQuotaExceeded,
    GatewayRequest,
    GatewayResult,
    GatewayStream,
    GatewayStreamEvent,
)

logger = structlog.get_logger(__name__)

# Operational gateway config keys (the tenant-level dict the route passes):
#   strategy                cost | latency | quality | pinned (default cost)
#   cheap_model             deployment for tiered simple queries
#   fallback_models         list of "provider:model" strings
#   tiered                  bool: tier simple vs complex queries
#   simple_query            bool: mark the request simple (tiered routing)
#   caching                 bool: allow exact/semantic caching (default True)
#   prompt_version          stable-prefix marker; bump to invalidate exact cache
#   cross_family_fallback   bool: allow content-policy cross-provider moves
#   quota_platform_usd / quota_tenant_usd / quota_surface_usd /
#   quota_end_user_usd      USD limits per budget level (0/absent = unlimited)
DEFAULT_GATEWAY_CFG: dict[str, Any] = {
    "strategy": "cost",
    "cheap_model": "gpt-4o-mini",
    "tiered": False,
    "caching": True,
    "prompt_version": "v1",
    "cross_family_fallback": True,
}


def quota_limits_from_cfg(gateway_cfg: dict | None) -> QuotaLimits:
    cfg = gateway_cfg or {}
    return QuotaLimits(
        platform_usd=float(cfg.get("quota_platform_usd") or 0.0),
        tenant_usd=float(cfg.get("quota_tenant_usd") or 0.0),
        surface_usd=float(cfg.get("quota_surface_usd") or 0.0),
        end_user_usd=float(cfg.get("quota_end_user_usd") or 0.0),
    )


class ProviderCallError(Exception):
    """One deployment attempt failed; carries the failure class."""

    def __init__(self, failure_class: str, provider: str, model: str, cause: Exception):
        self.failure_class = failure_class
        self.provider = provider
        self.model = model
        self.cause = cause
        super().__init__(f"{provider}:{model} {failure_class}: {cause}")


def _request_key(request: GatewayRequest) -> str:
    """Canonical exact-cache key material (messages + tools only)."""
    payload = {
        "messages": request.messages,
        "tools": request.tools,
        "structured_output": request.structured_output,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _to_llm_messages(messages: list[dict[str, Any]]) -> list[Any]:
    """Rebuild ``LLMMessage`` objects from the request dicts (metadata kept;
    the cache-control breakpoints need it)."""
    from backend.app.adapters.llm.provider import LLMMessage

    return [
        LLMMessage(
            role=m.get("role", "user"),
            content=m.get("content", ""),
            metadata=m.get("metadata") or {},
        )
        for m in messages
    ]


@dataclass(frozen=True)
class _AttemptOutcome:
    result: GatewayResult | None
    error: ProviderCallError | None


class Gateway:
    """The gateway interface (Arch 10, P3-9)."""

    def __init__(
        self,
        *,
        catalog: ModelCatalog | None = None,
        router: Router | None = None,
        cooldowns: RedisCooldownCache | None = None,
        quota: QuotaService | None = None,
        cache: GatewayCache | None = None,
        ledger: CostLedger | None = None,
        db: Any = None,
        queue: Any = None,
        key_resolver: Callable[[Any], Awaitable[str]] | None = None,
        adapter_factory: Callable[..., Any] | None = None,
    ):
        self.catalog = catalog or get_default_catalog()
        self.router = router or Router(self.catalog)
        self.cooldowns = cooldowns or RedisCooldownCache()
        self.quota = quota or QuotaService()
        self.cache = cache or GatewayCache(db=db)
        self.ledger = ledger or CostLedger(queue=queue, db=db)
        self._db = db
        self._queue = queue
        self._key_resolver = key_resolver or self._default_key_resolver
        self._adapter_factory = adapter_factory or self._default_adapter_factory
        self._adapters: dict[str, Any] = {}

    # -- lifecycle (managed-service convention) --------------------------------

    async def initialize(self) -> None:
        for svc in (
            self.cooldowns,
            self.quota,
            self.cache.exact,
            self.cache.semantic,
        ):
            await svc.initialize()

    async def close(self) -> None:
        for svc in (
            self.cooldowns,
            self.quota,
            self.cache.exact,
            self.cache.semantic,
        ):
            await svc.close()

    async def health_check(self) -> Any:
        """Aggregate gateway infrastructure health; each Redis-backed
        service reports DEGRADED when its backend is down (fail-open with
        a visible signal, never silent)."""
        from backend.app.infrastructure.patterns.health import (
            HealthComponent,
            HealthStatus,
        )

        deps = [
            await self.cooldowns.health_check(),
            await self.quota.health_check(),
            await self.cache.exact.health_check(),
            await self.cache.semantic.health_check(),
        ]
        severity = {HealthStatus.HEALTHY: 0, HealthStatus.DEGRADED: 1, HealthStatus.UNHEALTHY: 2}
        worst = max(severity[d.status] for d in deps)
        status = HealthStatus.HEALTHY if worst == 0 else HealthStatus.DEGRADED if worst == 1 else HealthStatus.UNHEALTHY
        return HealthComponent(
            name="gateway",
            status=status,
            message="gateway infra degraded" if worst > 0 else "",
            dependencies=deps,
        )

    # -- construction hooks (the only provider-coupled code) ----------------

    async def _default_key_resolver(self, tenant_config: Any) -> str:
        if self._db is None:
            raise GatewayConfigurationError(
                "gateway has no key resolver configured",
                provider=getattr(tenant_config, "default_provider", "openai"),
            )
        from backend.app.infrastructure.keys.service import (
            ProviderKeyNotFoundError,
            ProviderKeyService,
        )

        try:
            return await ProviderKeyService(self._db).resolve(tenant_config)
        except ProviderKeyNotFoundError as e:
            raise GatewayConfigurationError(
                str(e), provider=getattr(tenant_config, "default_provider", "openai")
            ) from e

    def _default_adapter_factory(
        self,
        tenant_config: Any,
        provider: str,
        model: str,
        request: GatewayRequest,
        api_key: str,
    ) -> Any:
        from backend.app.adapters.llm.provider import (
            LLMConfig,
            LLMProviderType,
            create_llm_adapter,
        )

        config = LLMConfig(
            model=model,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
            tools=list(request.tools),
            structured_output=request.structured_output,
        )
        return create_llm_adapter(LLMProviderType(provider), api_key, config)

    def _get_adapter(
        self,
        tenant_config: Any,
        provider: str,
        model: str,
        request: GatewayRequest,
        api_key: str,
    ) -> Any:
        key = f"{tenant_config.id}:{provider}:{model}"
        adapter = self._adapters.get(key)
        if adapter is None:
            adapter = self._adapter_factory(
                tenant_config, provider, model, request, api_key
            )
            self._adapters[key] = adapter
        else:
            adapter.config.tools = list(request.tools)
            adapter.config.structured_output = request.structured_output
        return adapter

    # -- public interface ---------------------------------------------------

    async def generate(
        self,
        request: GatewayRequest,
        tenant_config: Any,
        gateway_cfg: dict | None = None,
    ) -> GatewayResult:
        """Non-streaming generation: route -> quota -> cache -> execute with
        fallback -> ledger.

        Raises ``GatewayChainExhausted`` when every target failed for one
        failure class, ``GatewayQuotaExceeded`` when a budget level is
        over its limit, ``GatewayConfigurationError`` when the tenant has
        no usable deployment (no key, unknown provider).
        """
        cfg = dict(DEFAULT_GATEWAY_CFG)
        cfg.update(gateway_cfg or {})

        candidates = self.router.candidates(request, cfg)
        reservation = await self.quota.reserve(
            quota_limits_from_cfg(cfg),
            tenant_id=str(tenant_config.id),
            surface_id=request.surface_id,
            end_user_id=request.end_user_id,
            estimated_usd=await self._estimate_max_spend(candidates, request),
            request_id=request.request_id,
        )

        # Exact cache (no tools, no structured output).
        if (
            cfg.get("caching", True)
            and not request.tools
            and not request.structured_output
        ):
            cached = await self.cache.get_exact(
                tenant_id=str(tenant_config.id),
                prompt_version=str(cfg.get("prompt_version", "v1")),
                canonical_request=_request_key(request),
            )
            if cached is not None:
                await self.quota.release(reservation)
                return self._from_cache(cached)

        api_key = await self._key_resolver(tenant_config)
        outcome = await self._execute_chain(
            request, tenant_config, cfg, api_key, reservation
        )
        if outcome.error is not None:
            raise outcome.error.cause  # unreachable: chain raises on exhaustion
        return outcome.result  # type: ignore[return-value]

    async def stream(
        self,
        request: GatewayRequest,
        tenant_config: Any,
        gateway_cfg: dict | None = None,
    ) -> GatewayStream:
        """Streaming generation; yields normalized ``GatewayStreamEvent``.

        Fallback is transparent only before the first byte: once a
        ``delta`` or ``tool_use_*`` event was yielded, a failure surfaces
        as an ``error`` event (the connection tier decides how to
        degrade). Always ends with ``done`` (usage + finish_reason) or
        ``error``.
        """
        cfg = dict(DEFAULT_GATEWAY_CFG)
        cfg.update(gateway_cfg or {})
        return self._stream_impl(request, tenant_config, cfg)

    # -- routing helpers ------------------------------------------------------

    async def _decide(self, request: GatewayRequest, cfg: dict[str, Any]):
        """Availability snapshot (Redis cooldowns) + pure router decision."""
        candidates = self.router.candidates(request, cfg)
        snapshot: dict[tuple[str, str], bool] = {}
        for provider, model in candidates:
            snapshot[(provider, model)] = await self.cooldowns.is_available(
                provider, model
            )
        return self.router.decide(
            request, lambda p, m: snapshot.get((p, m), True), cfg
        )

    async def _estimate_max_spend(
        self, candidates: list[tuple[str, str]], request: GatewayRequest
    ) -> float:
        if not request.estimated_input_tokens:
            return 0.0
        costs = [
            self.catalog.estimate_cost(p, m, request.estimated_input_tokens, 1024)
            for p, m in candidates
        ]
        costs = [c for c in costs if c is not None]
        return max(costs, default=0.0)

    # -- execution ------------------------------------------------------------

    async def _execute_chain(
        self,
        request: GatewayRequest,
        tenant_config: Any,
        cfg: dict[str, Any],
        api_key: str,
        reservation: Any,
    ) -> _AttemptOutcome:
        decision = await self._decide(request, cfg)
        chain = build_fallback_chain(
            request,
            self.router.candidates(request, cfg),
            cfg,
            family_cross_fallback=bool(cfg.get("cross_family_fallback", True)),
        )
        seen: set[tuple[str, str]] = set()
        targets: list[tuple[str, str]] = [(decision.provider, decision.model)]
        last_error: ProviderCallError | None = None
        attempts = 0
        try:
            while targets:
                provider, model = targets.pop(0)
                if (provider, model) in seen:
                    continue
                seen.add((provider, model))
                attempts += 1
                outcome = await self._attempt(
                    request, tenant_config, api_key, provider, model, cfg
                )
                if outcome.result is not None:
                    await self.cooldowns.record_success(provider, model)
                    await self.quota.reconcile(
                        reservation, self._result_usd(outcome.result)
                    )
                    await self._after_success(request, cfg, outcome.result)
                    return _AttemptOutcome(outcome.result, None)
                last_error = outcome.error
                await self.cooldowns.record_failure(provider, model)
                targets = [
                    t
                    for t in chain.targets(last_error.failure_class)
                    if t not in seen
                ]
        except GatewayQuotaExceeded:
            await self.quota.release(reservation)
            raise
        except Exception:
            await self.quota.release(reservation)
            raise
        await self.quota.release(reservation)
        if last_error is not None:
            raise GatewayChainExhausted(
                last_error.provider,
                last_error.model,
                str(last_error.cause),
                failure_class=last_error.failure_class,
                attempts=attempts,
            ) from last_error.cause
        raise GatewayConfigurationError("no deployments to try")

    async def _attempt(
        self,
        request: GatewayRequest,
        tenant_config: Any,
        api_key: str,
        provider: str,
        model: str,
        cfg: dict[str, Any],
    ) -> _AttemptOutcome:
        """One deployment attempt. Returns an outcome; never raises for
        provider failures (they become ``ProviderCallError``)."""
        try:
            adapter = self._get_adapter(
                tenant_config, provider, model, request, api_key
            )
            messages = _to_llm_messages(request.messages)
            timeout = request.timeout_seconds or float(
                tenant_config.budgets.get("llm_timeout_seconds") or 60.0
            )
            start = time.perf_counter()
            response = await asyncio.wait_for(adapter.chat(messages), timeout=timeout)
            latency_ms = (time.perf_counter() - start) * 1000
            self.router.latency.update(provider, model, latency_ms)
            return _AttemptOutcome(
                GatewayResult(
                    content=response.content,
                    model=response.model or model,
                    provider=provider,
                    usage=response.usage or {},
                    finish_reason=response.finish_reason,
                    tool_calls=response.tool_calls,
                    routed_via=f"{provider}:{model}",
                    attempts=1,
                    latency_ms=latency_ms,
                    raw_response=response.raw_response,
                ),
                None,
            )
        except GatewayQuotaExceeded:
            raise
        except Exception as e:
            return _AttemptOutcome(
                None, ProviderCallError(classify_failure(e), provider, model, e)
            )

    def _result_usd(self, result: GatewayResult) -> float:
        usage = result.usage or {}
        return (
            self.catalog.estimate_cost(
                result.provider,
                result.model,
                usage.get("input_tokens", 0),
                usage.get("output_tokens", 0),
            )
            or 0.0
        )

    async def _after_success(
        self, request: GatewayRequest, cfg: dict[str, Any], result: GatewayResult
    ) -> None:
        """Cost ledger (P3-5) + exact-cache store. Failures never break the
        turn — logged, never silent."""
        try:
            await self.ledger.record(
                UsageRecord(
                    tenant_id=str(request.tenant_id),
                    provider=result.provider,
                    model=result.model,
                    input_tokens=(result.usage or {}).get("input_tokens", 0),
                    output_tokens=(result.usage or {}).get("output_tokens", 0),
                    reasoning_tokens=(result.usage or {}).get("reasoning_tokens", 0),
                    cached_tokens=(result.usage or {}).get("cached_tokens", 0),
                    usd=self._result_usd(result),
                    surface_id=request.surface_id,
                    end_user_id=request.end_user_id,
                    conversation_id=request.conversation_id,
                    session_id=request.session_id,
                    request_id=request.request_id,
                )
            )
        except Exception as e:
            logger.error("cost_ledger_record_failed", error=str(e))

        if (
            cfg.get("caching", True)
            and not request.tools
            and not request.structured_output
        ):
            try:
                await self.cache.set_exact(
                    tenant_id=str(request.tenant_id),
                    prompt_version=str(cfg.get("prompt_version", "v1")),
                    canonical_request=_request_key(request),
                    entry=CachedEntry(
                        provider=result.provider,
                        model=result.model,
                        content=result.content,
                        usage=result.usage or {},
                        finish_reason=result.finish_reason,
                        tool_calls=result.tool_calls,
                    ),
                )
            except Exception as e:
                logger.error("gateway_cache_store_failed", error=str(e))

    def _from_cache(self, entry: CachedEntry) -> GatewayResult:
        return GatewayResult(
            content=entry.content,
            model=entry.model,
            provider=entry.provider,
            usage=entry.usage,
            finish_reason=entry.finish_reason,
            tool_calls=entry.tool_calls,
            routed_via=f"cache:{entry.provider}:{entry.model}",
            attempts=0,
            cached=True,
        )

    # -- streaming -------------------------------------------------------------

    async def _stream_impl(
        self, request: GatewayRequest, tenant_config: Any, cfg: dict[str, Any]
    ) -> AsyncIterator[GatewayStreamEvent]:
        candidates = self.router.candidates(request, cfg)
        reservation = None
        finalized = False
        stream = None

        async def _release() -> None:
            """Idempotent reservation release (cancellation-safe; a
            reconciled reservation is never released again)."""
            nonlocal finalized
            if not finalized:
                finalized = True
                await self.quota.release(reservation)

        try:
            reservation = await self.quota.reserve(
                quota_limits_from_cfg(cfg),
                tenant_id=str(tenant_config.id),
                surface_id=request.surface_id,
                end_user_id=request.end_user_id,
            estimated_usd=await self._estimate_max_spend(candidates, request),
                request_id=request.request_id,
            )
            api_key = await self._key_resolver(tenant_config)
            decision = await self._decide(request, cfg)
            chain = build_fallback_chain(
                request,
                candidates,
                cfg,
                family_cross_fallback=bool(cfg.get("cross_family_fallback", True)),
            )

            seen: set[tuple[str, str]] = set()
            targets: list[tuple[str, str]] = [(decision.provider, decision.model)]
            last_error: ProviderCallError | None = None
            emitted = False

            while targets:
                provider, model = targets.pop(0)
                if (provider, model) in seen:
                    continue
                seen.add((provider, model))
                adapter = self._get_adapter(
                    tenant_config, provider, model, request, api_key
                )
                messages = _to_llm_messages(request.messages)
                timeout = request.timeout_seconds or float(
                    tenant_config.budgets.get("llm_timeout_seconds") or 60.0
                )
                usage: dict[str, int] = {}
                finish_reason: str | None = None
                parts: list[str] = []
                tool_acc: dict[int, dict[str, Any]] = {}
                start = time.perf_counter()
                try:
                    stream = adapter.stream(messages)  # async generator
                    # Timeout the connection/setup phase (first event).
                    first = await asyncio.wait_for(stream.__anext__(), timeout=timeout)

                    async def _handle(ev: Any) -> bool:
                        """Process one event; True when a delta/tool byte
                        was emitted (disables transparent failover)."""
                        nonlocal usage, finish_reason, parts, tool_acc, emitted
                        etype = ev.type
                        if etype == "delta" and ev.content:
                            emitted = True
                            parts.append(ev.content)
                            yield_event = GatewayStreamEvent(type="delta", content=ev.content)
                        elif etype == "tool_use_start":
                            emitted = True
                            tool_acc.setdefault(ev.index or 0, {"id": "", "name": "", "args": ""})
                            entry = tool_acc[ev.index or 0]
                            if ev.id:
                                entry["id"] = ev.id
                            if ev.name:
                                entry["name"] = ev.name
                            yield_event = GatewayStreamEvent(
                                type="tool_use_start", index=ev.index, id=entry["id"], name=entry["name"]
                            )
                        elif etype == "tool_use_delta":
                            entry = tool_acc.setdefault(ev.index or 0, {"id": "", "name": "", "args": ""})
                            if ev.id:
                                entry["id"] = ev.id
                            if ev.name:
                                entry["name"] = ev.name
                            if ev.arguments:
                                entry["args"] += ev.arguments
                            yield_event = GatewayStreamEvent(
                                type="tool_use_delta", index=ev.index, id=entry["id"], args=ev.arguments
                            )
                        elif etype == "tool_use_end":
                            entry = tool_acc.get(ev.index or 0, {})
                            if ev.id:
                                entry["id"] = ev.id
                            if ev.name:
                                entry["name"] = ev.name
                            yield_event = GatewayStreamEvent(
                                type="tool_use_end",
                                index=ev.index,
                                id=entry.get("id"),
                                name=entry.get("name"),
                                arguments=Gateway._decode_args(entry.get("args", "")),
                            )
                        elif etype == "usage" and ev.usage:
                            usage = ev.usage
                            yield_event = None
                        elif etype == "done":
                            finish_reason = ev.finish_reason
                            if ev.usage:
                                usage = ev.usage
                            yield_event = None
                        elif etype == "error":
                            raise ProviderCallError(
                                classify_failure(RuntimeError(ev.error or "stream error")),
                                provider,
                                model,
                                RuntimeError(ev.error or "stream error"),
                            )
                        else:
                            yield_event = None
                        if yield_event is not None:
                            yield_out = yield_event
                            return yield_out
                        return None

                    first_out = await _handle(first)
                    if first_out is not None:
                        yield first_out
                    async for event in stream:
                        out = await _handle(event)
                        if out is not None:
                            yield out
                except (asyncio.CancelledError, GeneratorExit):
                    # Disconnect / consumer abort (Phase 4): cancel upstream
                    # so the provider stops billing tokens and no in-flight
                    # generation is orphaned, then release the quota hold.
                    if stream is not None:
                        try:
                            await stream.aclose()
                        except Exception:
                            pass
                    await _release()
                    raise
                except Exception as e:
                    if isinstance(e, ProviderCallError):
                        last_error = e
                    else:
                        last_error = ProviderCallError(
                            classify_failure(e), provider, model, e
                        )
                    await self.cooldowns.record_failure(provider, model)
                    if emitted:
                        # Mid-stream failure: no transparent failover.
                        yield GatewayStreamEvent(
                            type="error",
                            error=str(e),
                            failure_class=last_error.failure_class,
                            provider=provider,
                            model=model,
                        )
                        await _release()
                        return
                    targets = [
                        t
                        for t in chain.targets(last_error.failure_class)
                        if t not in seen
                    ]
                    continue

                # Success.
                await self.cooldowns.record_success(provider, model)
                latency_ms = (time.perf_counter() - start) * 1000
                self.router.latency.update(provider, model, latency_ms)
                result = GatewayResult(
                    content="".join(parts),
                    model=model,
                    provider=provider,
                    usage=usage,
                    finish_reason=finish_reason,
                    tool_calls=Gateway._finalize_tool_calls(tool_acc),
                    routed_via=f"{provider}:{model}",
                    attempts=len(seen),
                    latency_ms=latency_ms,
                )
                await self.quota.reconcile(reservation, self._result_usd(result))
                finalized = True
                await self._after_success(request, cfg, result)
                yield GatewayStreamEvent(type="usage", usage=usage, provider=provider, model=model)
                yield GatewayStreamEvent(
                    type="done", finish_reason=finish_reason, usage=usage, provider=provider, model=model
                )
                return

            # Chain exhausted before any byte.
            await _release()
            raise GatewayChainExhausted(
                last_error.provider if last_error else "unknown",
                last_error.model if last_error else "unknown",
                str(last_error.cause) if last_error else "no deployments",
                failure_class=last_error.failure_class if last_error else "general",
                attempts=len(seen),
            )
        except GatewayQuotaExceeded as e:
            await _release()
            yield GatewayStreamEvent(
                type="error", error=str(e), failure_class="quota_exceeded"
            )
        except GatewayConfigurationError as e:
            await _release()
            yield GatewayStreamEvent(
                type="error", error=str(e), failure_class="configuration"
            )
        except GatewayChainExhausted as e:
            yield GatewayStreamEvent(type="error", error=str(e), failure_class=e.failure_class)
        except Exception as e:
            await _release()
            logger.error("gateway_stream_failed", error=str(e))
            yield GatewayStreamEvent(type="error", error=str(e), failure_class="general")
        except BaseException:
            # Cancellation / generator close between or at yield points:
            # close any in-flight upstream stream (connection tier abort
            # cancels the provider), release the quota hold, re-raise.
            if stream is not None:
                try:
                    await stream.aclose()
                except Exception:
                    pass
            await _release()
            raise

    @staticmethod
    def _decode_args(raw: str) -> dict[str, Any] | None:
        try:
            decoded: Any = json.loads(raw) if raw else {}
            return decoded if isinstance(decoded, dict) else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _finalize_tool_calls(
        tool_acc: dict[int, dict[str, Any]]
    ) -> list[dict[str, Any]] | None:
        calls: list[dict[str, Any]] = []
        for index in sorted(tool_acc):
            entry = tool_acc[index]
            if not entry.get("id") or not entry.get("name"):
                return None
            arguments = Gateway._decode_args(entry.get("args", ""))
            if arguments is None:
                return None
            calls.append(
                {"id": entry["id"], "name": entry["name"], "arguments": arguments}
            )
        return calls or None


# -- module singleton (codebase pattern: init_* / get_*) ----------------------

class GatewayBackedAdapter:
    """``BaseLLMAdapter``-contract facade over the gateway (P3-9).

    Lets components built on the pre-gateway adapter contract (compaction
    summarizer, eval) run through the gateway: same routing, quota, ledger
    and caching path, with zero key handling in the caller. ``chat`` is
    translated to ``generate``; the response is rebuilt as ``LLMResponse``
    so downstream code (``response.content``, usage dicts) is unchanged.
    """

    def __init__(
        self,
        gateway: "Gateway",
        tenant_config: Any,
        provider: str,
        model: str,
    ):
        from backend.app.adapters.llm.provider import (
            LLMConfig,
            LLMProviderType,
        )

        self._gateway = gateway
        self._tenant_config = tenant_config
        self._provider = LLMProviderType(provider)
        self.config = LLMConfig(model=model)

    @property
    def provider_type(self) -> Any:
        return self._provider

    async def chat(self, messages: list[Any]) -> Any:
        from backend.app.adapters.llm.provider import LLMResponse

        request = GatewayRequest(
            tenant_id=str(self._tenant_config.id),
            messages=[
                {"role": m.role, "content": m.content, "metadata": m.metadata}
                for m in messages
            ],
            request_id=str(uuid4()),
            provider=self._provider.value,
            model=self.config.model,
            strategy="cost",
        )
        result = await self._gateway.generate(
            request, self._tenant_config, None
        )
        return LLMResponse(
            content=result.content,
            model=result.model,
            usage=result.usage or {},
            finish_reason=result.finish_reason,
        )

    async def stream(self, messages: list[Any]) -> AsyncIterator[Any]:
        raise NotImplementedError(
            "GatewayBackedAdapter is chat-only (summarizer path)"
        )

    async def stream_chat(self, messages: list[Any]) -> Any:
        raise NotImplementedError(
            "GatewayBackedAdapter is chat-only (summarizer path)"
        )


_gateway: Gateway | None = None

def init_gateway(
    *,
    db: Any = None,
    queue: Any = None,
    key_resolver: Callable[[Any], Awaitable[str]] | None = None,
    adapter_factory: Callable[..., Any] | None = None,
    catalog: ModelCatalog | None = None,
    cooldowns: RedisCooldownCache | None = None,
    quota: QuotaService | None = None,
    cache: GatewayCache | None = None,
    ledger: CostLedger | None = None,
) -> Gateway:
    global _gateway
    _gateway = Gateway(
        db=db,
        queue=queue,
        key_resolver=key_resolver,
        adapter_factory=adapter_factory,
        catalog=catalog,
        cooldowns=cooldowns,
        quota=quota,
        cache=cache,
        ledger=ledger,
    )
    return _gateway


def get_gateway() -> Gateway:
    if _gateway is None:
        raise RuntimeError("gateway not initialized (call init_gateway first)")
    return _gateway
