"""
Gateway LLM client foundation (Arch 10, P0-6).

Wraps every provider call with a per-call timeout, bounded retries with
backoff, and a per-(tenant, provider, model) circuit breaker. The breaker
is in-process for now; Redis-shared breaker state lands in P3-4.

After retries are exhausted or the breaker is open, raises
``LLMChainExhaustedError`` so the turn degrades (queue / escalate /
offline capture — tenant-configurable) instead of failing open.
"""

import asyncio
import structlog
from dataclasses import dataclass
from typing import Any

from backend.app.adapters.llm.provider import (
    BaseLLMAdapter,
    LLMConfig,
    LLMMessage,
    LLMResponse,
)
from backend.app.infrastructure.patterns.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitBreakerOpenError,
)
from backend.app.infrastructure.patterns.retry import (
    AsyncRetry,
    ExponentialBackoff,
    RetryConfig,
)

logger = structlog.get_logger(__name__)


class LLMCallError(Exception):
    """A provider call failed in a non-retryable way."""


class LLMChainExhaustedError(Exception):
    """The fallback chain is exhausted: retries failed and/or breaker open.

    The turn must degrade per tenant configuration, never fail open.
    """

    def __init__(self, provider: str, model: str, reason: str):
        self.provider = provider
        self.model = model
        self.reason = reason
        super().__init__(f"llm_chain_exhausted provider={provider} model={model}: {reason}")


@dataclass
class GatewayClientConfig:
    call_timeout_seconds: float = 60.0
    retry_max_attempts: int = 3
    retry_min_delay: float = 0.5
    breaker_failure_threshold: int = 5
    breaker_recovery_timeout: float = 30.0


def _retryable_exceptions() -> tuple[type[Exception], ...]:
    excs: list[type[Exception]] = [ConnectionError, TimeoutError, OSError]
    try:
        import httpx

        excs.append(httpx.TransportError)
    except ImportError:
        pass
    try:
        from openai import APIConnectionError, APITimeoutError

        excs.extend([APIConnectionError, APITimeoutError])
    except ImportError:
        pass
    return tuple(excs)


class ResilientLLMClient:
    """Timeout + bounded retry + circuit breaker around a provider adapter."""

    def __init__(
        self,
        adapter: BaseLLMAdapter,
        config: GatewayClientConfig | None = None,
    ):
        self.adapter = adapter
        self.config = config or GatewayClientConfig()
        key = f"{adapter.provider_type.value}:{adapter.config.model}"
        self._breaker = CircuitBreaker(
            CircuitBreakerConfig(
                name=key,
                failure_threshold=self.config.breaker_failure_threshold,
                recovery_timeout=self.config.breaker_recovery_timeout,
            )
        )
        self._retry = AsyncRetry(
            RetryConfig(
                max_attempts=self.config.retry_max_attempts,
                backoff=ExponentialBackoff(min_delay=self.config.retry_min_delay),
                retryable_exceptions=_retryable_exceptions(),
            )
        )

    async def chat(
        self, messages: list[LLMMessage], timeout_seconds: float | None = None
    ) -> LLMResponse:
        return await self._call("chat", messages, timeout_seconds)

    async def stream_chat(
        self, messages: list[LLMMessage], timeout_seconds: float | None = None
    ) -> Any:
        return await self._call("stream_chat", messages, timeout_seconds)

    async def _call(
        self, method: str, messages: list[LLMMessage], timeout_seconds: float | None
    ) -> Any:
        timeout = timeout_seconds or self.config.call_timeout_seconds
        fn = getattr(self.adapter, method)

        async def attempt() -> Any:
            return await asyncio.wait_for(fn(messages), timeout=timeout)

        try:
            return await self._breaker.call(self._retry.execute, attempt)
        except CircuitBreakerOpenError as e:
            raise LLMChainExhaustedError(
                self.adapter.provider_type.value,
                self.adapter.config.model,
                f"circuit_breaker_open: {e}",
            ) from e
        except LLMChainExhaustedError:
            raise
        except Exception as e:
            raise LLMChainExhaustedError(
                self.adapter.provider_type.value,
                self.adapter.config.model,
                str(e),
            ) from e

    def get_state_summary(self) -> dict[str, Any]:
        return self._breaker.get_state_summary()


_clients: dict[str, ResilientLLMClient] = {}


def get_llm_client(
    tenant_id: str,
    adapter: BaseLLMAdapter,
    config: GatewayClientConfig | None = None,
) -> ResilientLLMClient:
    """Return the cached per-(tenant, provider, model) resilient client.

    In-process registry for now (L1); Redis-shared breaker state in P3-4.
    Key rotation (P0-8) rotates the tenant's provider key, which changes
    the client's identity and forces a fresh instance.
    """
    key = f"{tenant_id}:{adapter.provider_type.value}:{adapter.config.model}"
    client = _clients.get(key)
    if client is None:
        client = ResilientLLMClient(adapter, config=config)
        _clients[key] = client
    return client
