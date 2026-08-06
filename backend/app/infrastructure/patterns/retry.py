import asyncio
import structlog
from dataclasses import dataclass, field
from typing import Awaitable, Callable, TypeVar, Optional
from functools import wraps

logger = structlog.get_logger(__name__)

T = TypeVar("T")


class RetryExhaustedError(Exception):
    def __init__(self, attempts: int, last_error: Exception):
        self.attempts = attempts
        self.last_error = last_error
        super().__init__(f"Retry exhausted after {attempts} attempts: {last_error}")


class NonRetryableError(Exception):
    pass


@dataclass
class ExponentialBackoff:
    min_delay: float = 1.0
    max_delay: float = 60.0
    jitter: float = 0.1
    multiplier: float = 2.0

    def delay(self, attempt: int) -> float:
        import random
        delay = min(self.min_delay * (self.multiplier ** attempt), self.max_delay)
        jitter_amount = delay * self.jitter
        return delay + random.uniform(-jitter_amount, jitter_amount)


@dataclass
class RetryConfig:
    max_attempts: int = 3
    backoff: ExponentialBackoff = field(default_factory=ExponentialBackoff)
    retryable_exceptions: tuple = (ConnectionError, TimeoutError, OSError)
    on_retry: Optional[Callable[[int, Exception], None]] = None


async def retry_async(
    fn: Callable[..., Awaitable[T]],
    *args,
    config: Optional[RetryConfig] = None,
    **kwargs,
) -> T:
    cfg = config or RetryConfig()
    last_error: Optional[Exception] = None

    for attempt in range(cfg.max_attempts):
        try:
            return await fn(*args, **kwargs)
        except NonRetryableError:
            raise
        except cfg.retryable_exceptions as e:
            last_error = e
            if attempt == cfg.max_attempts - 1:
                raise RetryExhaustedError(cfg.max_attempts, e) from e
            delay = cfg.backoff.delay(attempt)
            logger.warning("retry_attempt", attempt=attempt + 1, max_attempts=cfg.max_attempts, delay=delay, error=str(e))
            if cfg.on_retry:
                cfg.on_retry(attempt + 1, e)
            await asyncio.sleep(delay)
        except Exception as e:
            last_error = e
            raise

    raise RetryExhaustedError(cfg.max_attempts, last_error)  # type: ignore[arg-type]


class AsyncRetry:
    def __init__(self, config: Optional[RetryConfig] = None):
        self.config = config or RetryConfig()

    async def execute(self, fn: Callable[..., Awaitable[T]], *args, **kwargs) -> T:
        return await retry_async(fn, *args, config=self.config, **kwargs)

    def __call__(self, fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @wraps(fn)
        async def wrapper(*args, **kwargs) -> T:
            return await self.execute(fn, *args, **kwargs)
        return wrapper
