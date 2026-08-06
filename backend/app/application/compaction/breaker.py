"""
Compaction circuit breaker (Arch 8.2, P2-4 — Neryva addition).

Per-session pause-after-N-consecutive-failures (Claude Code semantics,
corroborated by the pi-ultra-compact reference implementation): when a
thread's compaction fails 3 times in a row, the breaker trips and the
CompactionService falls back to lossy truncation (keep the newest K turns,
drop the middle) with a visible degraded marker — never silent.

State is in-process (module-level registry), mirroring the LLM breaker
(P0-11): shared across replicas moves to Redis in P3-4 with the shared
breaker layer. Recovery: after ``recovery_timeout`` the breaker allows a
fresh attempt (half-open equivalent); one success resets the counter.
"""

from __future__ import annotations

import asyncio
import structlog
import time
from dataclasses import dataclass, field

logger = structlog.get_logger(__name__)


@dataclass
class CompactionBreakerConfig:
    max_failures: int = 3
    recovery_timeout: float = 300.0
    state: dict[str, dict[str, float | int]] = field(default_factory=dict, repr=False)


class CompactionBreaker:
    """Per-thread consecutive-failure gate with timeout recovery."""

    def __init__(self, config: CompactionBreakerConfig | None = None):
        self.config = config or CompactionBreakerConfig()
        self._lock = asyncio.Lock()
        # key -> (consecutive_failures, tripped_at | None)
        self._state: dict[str, list[object]] = {}

    async def record_failure(self, key: str) -> int:
        """Count a compaction failure; returns the new consecutive count."""
        async with self._lock:
            failures, _ = self._state.get(key, [0, None])
            failures = int(failures) + 1
            self._state[key] = [failures, None]
            if failures >= self.config.max_failures:
                self._state[key][1] = time.monotonic()
                logger.warning(
                    "compaction_breaker_tripped",
                    key=key,
                    failures=failures,
                )
            return failures

    async def record_success(self, key: str) -> None:
        """A compacted checkpoint lands: reset the thread's counter."""
        async with self._lock:
            self._state.pop(key, None)

    async def is_tripped(self, key: str) -> bool:
        """True while the thread's breaker is open (fallback in effect)."""
        async with self._lock:
            entry = self._state.get(key)
            if not entry:
                return False
            failures, tripped_at = entry
            if tripped_at is None:
                return False
            if time.monotonic() - float(tripped_at) >= self.config.recovery_timeout:
                self._state.pop(key, None)
                logger.info("compaction_breaker_recovered", key=key)
                return False
            return True

    def state_summary(self, key: str | None = None) -> dict:
        """Observability surface (operator UI reads this)."""
        if key is not None:
            failures, tripped_at = self._state.get(key, [0, None])
            return {
                "key": key,
                "consecutive_failures": failures,
                "tripped": tripped_at is not None,
            }
        return {
            key: {
                "consecutive_failures": failures,
                "tripped": tripped_at is not None,
            }
            for key, (failures, tripped_at) in self._state.items()
        }


_default_breaker = CompactionBreaker()


def get_compaction_breaker() -> CompactionBreaker:
    """Shared in-process breaker registry (Redis-shared in P3-4)."""
    return _default_breaker
