import asyncio
import structlog
import time
from dataclasses import dataclass
from enum import Enum, auto
from typing import Awaitable, Callable, Optional, TypeVar

logger = structlog.get_logger(__name__)

T = TypeVar("T")


class CircuitState(Enum):
    CLOSED = auto()
    OPEN = auto()
    HALF_OPEN = auto()


class CircuitBreakerOpenError(Exception):
    def __init__(self, name: str, until: float):
        self.name = name
        self.until = until
        super().__init__(f"Circuit breaker '{name}' is open until {until}")


@dataclass
class CircuitBreakerConfig:
    name: str = "default"
    failure_threshold: int = 5
    recovery_timeout: float = 30.0
    half_open_max_calls: int = 3
    consecutive_successes_to_close: int = 2


class CircuitBreaker:
    def __init__(self, config: Optional[CircuitBreakerConfig] = None):
        self.config = config or CircuitBreakerConfig()
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._half_open_calls = 0
        self._last_failure_time: float = 0.0
        self._lock = asyncio.Lock()

    @property
    def state(self) -> CircuitState:
        return self._state

    async def call(self, fn: Callable[..., Awaitable[T]], *args, **kwargs) -> T:
        async with self._lock:
            if self._state == CircuitState.OPEN:
                if time.monotonic() - self._last_failure_time >= self.config.recovery_timeout:
                    self._state = CircuitState.HALF_OPEN
                    self._half_open_calls = 0
                    self._success_count = 0
                    logger.info("circuit_breaker_half_open", name=self.config.name)
                else:
                    raise CircuitBreakerOpenError(self.config.name, self._last_failure_time + self.config.recovery_timeout)

            if self._state == CircuitState.HALF_OPEN and self._half_open_calls >= self.config.half_open_max_calls:
                raise CircuitBreakerOpenError(self.config.name, time.monotonic() + 5.0)

        try:
            result = await fn(*args, **kwargs)
            await self._record_success()
            return result
        except Exception as e:
            await self._record_failure(e)
            raise

    async def _record_success(self) -> None:
        async with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                self._success_count += 1
                if self._success_count >= self.config.consecutive_successes_to_close:
                    self._state = CircuitState.CLOSED
                    self._failure_count = 0
                    self._success_count = 0
                    self._half_open_calls = 0
                    logger.info("circuit_breaker_closed", name=self.config.name)
            elif self._state == CircuitState.CLOSED:
                self._failure_count = 0

    async def _record_failure(self, error: Exception) -> None:
        async with self._lock:
            self._last_failure_time = time.monotonic()
            if self._state == CircuitState.HALF_OPEN:
                self._state = CircuitState.OPEN
                self._half_open_calls = 0
                self._success_count = 0
                logger.warning("circuit_breaker_reopened", name=self.config.name, error=str(error))
            elif self._state == CircuitState.CLOSED:
                self._failure_count += 1
                if self._failure_count >= self.config.failure_threshold:
                    self._state = CircuitState.OPEN
                    logger.warning("circuit_breaker_opened", name=self.config.name, failures=self._failure_count)

    def get_state_summary(self) -> dict:
        return {
            "name": self.config.name,
            "state": self._state.name,
            "failure_count": self._failure_count,
            "success_count": self._success_count,
            "failure_threshold": self.config.failure_threshold,
            "recovery_timeout": self.config.recovery_timeout,
        }
