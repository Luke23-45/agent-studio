import asyncio
import json
import time as time_module
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

import structlog

from ..patterns import HealthComponent, HealthStatus, ManagedService
from ..patterns.circuit_breaker import CircuitBreaker, CircuitBreakerConfig
from ..patterns.retry import AsyncRetry, ExponentialBackoff, RetryConfig

logger = structlog.get_logger(__name__)


@dataclass
class Job:
    id: str = field(default_factory=lambda: str(uuid4()))
    type: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    priority: int = 0
    scheduled_at: datetime | None = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    retries: int = 0
    max_retries: int = 3
    error: str | None = None
    trace_id: str = ""


@dataclass
class QueueConfig:
    redis_url: str = "redis://localhost:6379"
    prefix: str = "neryva:queue:"
    dead_letter_prefix: str = "neryva:dead:"
    max_retries: int = 3
    retry_min_delay: float = 1.0
    poll_interval: float = 1.0
    batch_size: int = 10
    circuit_breaker_failures: int = 3
    circuit_breaker_recovery: float = 15.0
    idempotency_ttl_seconds: int = 86400


class QueueManager(ManagedService):
    def __init__(self, config: QueueConfig | None = None):
        super().__init__("queue")
        self.config = config or QueueConfig()
        self._client: Any = None
        self._handlers: dict[str, Callable] = {}
        self._running = False
        # In-memory idempotency tracking (Redis path caches seen keys lazily
        # at enqueue time; TTL applies on the Redis side)
        self._idem_seen: set[str] = set()
        self._idem_processed: set[str] = set()
        self._dead_letters: list[str] = []
        self._retry = AsyncRetry(RetryConfig(
            max_attempts=self.config.max_retries,
            backoff=ExponentialBackoff(min_delay=self.config.retry_min_delay),
        ))
        self._circuit_breaker = CircuitBreaker(CircuitBreakerConfig(
            name="queue",
            failure_threshold=self.config.circuit_breaker_failures,
            recovery_timeout=self.config.circuit_breaker_recovery,
        ))

    async def _do_initialize(self) -> None:
        try:
            import redis.asyncio as redis
            self._client = redis.from_url(self.config.redis_url, decode_responses=True, retry_on_timeout=True)
            await self._client.ping()
            logger.info("queue_initialized", url=self.config.redis_url)
        except Exception as e:
            # Redis missing OR unreachable: degrade to the in-memory queue
            # (single process only) instead of crashing the process at startup.
            logger.warning(
                "redis_unavailable_falling_back_to_memory",
                url=self.config.redis_url,
                error=str(e),
            )
            self._client = None
            self._in_memory_queue: list[str] = []

    async def _do_close(self) -> None:
        self._running = False
        if self._client:
            await self._client.close()
            self._client = None

    def register_handler(self, job_type: str, handler: Callable) -> None:
        self._handlers[job_type] = handler
        logger.info("handler_registered", job_type=job_type)

    def unregister_handler(self, job_type: str) -> None:
        self._handlers.pop(job_type, None)

    def _queue_key(self, priority: int = 0) -> str:
        if priority > 0:
            return f"{self.config.prefix}high"
        elif priority < 0:
            return f"{self.config.prefix}low"
        return f"{self.config.prefix}default"

    def _dead_key(self, job_type: str) -> str:
        return f"{self.config.dead_letter_prefix}{job_type}"

    async def enqueue(self, job: Job, idempotency_key: str | None = None) -> bool:
        """Enqueue a job.

        With ``idempotency_key`` the job is enqueued at most once per key
        (atomic Redis SET NX with TTL when connected, in-memory set otherwise).
        Returns True when accepted or already seen/processed.
        """
        if idempotency_key:
            if not await self._claim_idempotency(idempotency_key):
                logger.info("job_duplicate_skipped", job_type=job.type, idempotency_key=idempotency_key)
                return True
            job.payload["_idem"] = idempotency_key

        if self._client:
            try:
                await self._circuit_breaker.call(self._redis_enqueue, job)
                return True
            except Exception as e:
                logger.error("enqueue_failed", job_type=job.type, error=str(e))
                return False
        else:
            self._in_memory_queue.append(self._serialize_job(job))
            return True

    async def _claim_idempotency(self, idempotency_key: str) -> bool:
        """Atomically claim an idempotency key. Returns True when this caller
        won the claim (job should be enqueued), False when already claimed."""
        if self._client:
            key = f"{self.config.prefix}idem:{idempotency_key}"
            claimed = await self._client.set(
                key, "1", ex=self.config.idempotency_ttl_seconds, nx=True
            )
            return claimed is not None
        if idempotency_key in self._idem_seen:
            return False
        self._idem_seen.add(idempotency_key)
        return True

    async def _redis_enqueue(self, job: Job) -> None:
        serialized = self._serialize_job(job)
        queue_key = self._queue_key(job.priority)

        if job.scheduled_at and job.scheduled_at > datetime.utcnow():
            await self._client.zadd(f"{queue_key}:scheduled", {serialized: job.scheduled_at.timestamp()})
        else:
            await self._client.lpush(queue_key, serialized)

    def _serialize_job(self, job: Job) -> str:
        return json.dumps({
            "id": job.id,
            "type": job.type,
            "payload": job.payload,
            "priority": job.priority,
            "scheduled_at": job.scheduled_at.isoformat() if job.scheduled_at else None,
            "created_at": job.created_at.isoformat(),
            "retries": job.retries,
            "max_retries": job.max_retries,
            "error": job.error,
            "trace_id": job.trace_id,
        })

    def _deserialize_job(self, data: str) -> Job:
        d = json.loads(data)
        return Job(
            id=d["id"], type=d["type"], payload=d.get("payload", {}),
            priority=d.get("priority", 0),
            scheduled_at=datetime.fromisoformat(d["scheduled_at"]) if d.get("scheduled_at") else None,
            created_at=datetime.fromisoformat(d["created_at"]),
            retries=d.get("retries", 0), max_retries=d.get("max_retries", 3),
            error=d.get("error"), trace_id=d.get("trace_id", ""),
        )

    async def dequeue(self, timeout: float = 1.0) -> Job | None:
        if self._client:
            try:
                return await self._circuit_breaker.call(self._redis_dequeue, timeout)
            except Exception:
                return None
        if self._in_memory_queue:
            return self._deserialize_job(self._in_memory_queue.pop(0))
        # Poll instead of hot-spinning while the in-memory queue is empty
        await asyncio.sleep(timeout)
        return None

    async def _redis_dequeue(self, timeout: float) -> Job | None:
        await self._move_scheduled_jobs()

        for queue_key in [f"{self.config.prefix}high", f"{self.config.prefix}default", f"{self.config.prefix}low"]:
            result = await self._client.brpop(queue_key, timeout=int(timeout))
            if result:
                _, serialized = result
                return self._deserialize_job(serialized)
        return None

    async def _move_scheduled_jobs(self) -> None:
        now = time_module.time()
        for priority_key in ["high", "default", "low"]:
            scheduled_key = f"{self.config.prefix}{priority_key}:scheduled"
            queue_key = f"{self.config.prefix}{priority_key}"
            jobs = await self._client.zrangebyscore(scheduled_key, 0, now)
            if jobs:
                pipe = self._client.pipeline()
                for job_data in jobs:
                    pipe.lpush(queue_key, job_data)
                    pipe.zrem(scheduled_key, job_data)
                await pipe.execute()

    async def process_job(self, job: Job) -> bool:
        idem_key = job.payload.get("_idem") if isinstance(job.payload, dict) else None
        if idem_key and idem_key in self._idem_processed:
            logger.info("job_already_processed_skipped", job_id=job.id, job_type=job.type)
            return True

        handler = self._handlers.get(job.type)
        if not handler:
            logger.warning("no_handler_for_job", job_type=job.type, job_id=job.id)
            await self._send_to_dead_letter(job, "No handler registered")
            return False

        try:
            await handler(job.payload)
            if idem_key:
                self._idem_processed.add(idem_key)
            logger.info("job_processed", job_id=job.id, job_type=job.type)
            return True
        except Exception as e:
            logger.error("job_processing_error", job_id=job.id, job_type=job.type, error=str(e))
            job.error = str(e)
            job.retries += 1
            if job.retries < job.max_retries:
                backoff = ExponentialBackoff(min_delay=self.config.retry_min_delay)
                delay = backoff.delay(job.retries)
                job.scheduled_at = datetime.utcnow() + timedelta(seconds=delay)
                await self.enqueue(job)
                logger.info("job_requeued", job_id=job.id, retry=job.retries, delay=delay)
            else:
                await self._send_to_dead_letter(job, str(e))
                logger.error("job_failed_permanently", job_id=job.id, job_type=job.type)
            return False

    async def _send_to_dead_letter(self, job: Job, reason: str) -> None:
        serialized = self._serialize_job(job)
        if not self._client:
            self._dead_letters.append(serialized)
            logger.info("job_dead_lettered", job_id=job.id, job_type=job.type, reason=reason)
            return
        try:
            dead_key = self._dead_key(job.type)
            await self._client.lpush(dead_key, serialized)
            logger.info("job_dead_lettered", job_id=job.id, job_type=job.type, reason=reason)
        except Exception as e:
            logger.error("dead_letter_failed", job_id=job.id, error=str(e))

    async def process_loop(self, exit_event=None) -> None:
        self._running = True
        logger.info("queue_processor_started")
        while self._running:
            if exit_event and exit_event.is_set():
                break
            await self._update_prometheus_gauges()
            job = await self.dequeue(timeout=self.config.poll_interval)
            if job:
                await self.process_job(job)

    async def _update_prometheus_gauges(self) -> None:
        """Publish live queue depth + DLQ size gauges (P6-2)."""
        try:
            from backend.app.infrastructure.observability.metrics import get_metrics

            metrics = get_metrics()
            try:
                depth = await self.get_queue_length()
            except Exception:
                depth = -1
            try:
                dlq = await self.get_dead_letter_count()
            except Exception:
                dlq = -1
            metrics.queue_depth.labels(queue_name="default").set(depth)
            metrics.dlq_size.labels(queue_name="default").set(dlq)
        except Exception:  # pragma: no cover - defensive
            pass

    def stop_processing(self) -> None:
        self._running = False

    async def get_queue_length(self, priority: int | None = None) -> int:
        if not self._client:
            return len(self._in_memory_queue)
        key = self._queue_key(priority or 0)
        return await self._client.llen(key)

    async def get_dead_letter_count(self, job_type: str | None = None) -> int:
        if not self._client:
            if job_type:
                return sum(
                    1
                    for item in self._dead_letters
                    if self._deserialize_job(item).type == job_type
                )
            return len(self._dead_letters)
        pattern = self._dead_key(job_type or "*")
        cursor = 0
        total = 0
        while True:
            cursor, keys = await self._client.scan(cursor, match=pattern, count=100)
            for key in keys:
                total += await self._client.llen(key)
            if cursor == 0:
                break
        return total

    async def drain_dead_letter(self, job_type: str | None = None, limit: int = 100) -> list[Job]:
        """Remove and return failed jobs from the dead-letter queue.

        Used by the cleanup job to inspect and/or requeue dead letters.
        """
        drained: list[Job] = []
        if not self._client:
            if job_type:
                remaining: list[str] = []
                for item in self._dead_letters:
                    job = self._deserialize_job(item)
                    if job.type == job_type and len(drained) < limit:
                        drained.append(job)
                    else:
                        remaining.append(item)
                self._dead_letters = remaining
            else:
                for item in list(self._dead_letters[:limit]):
                    drained.append(self._deserialize_job(item))
                self._dead_letters = self._dead_letters[limit:]
            return drained

        keys: list[str] = []
        if job_type:
            keys = [self._dead_key(job_type)]
        else:
            cursor = 0
            while True:
                cursor, found = await self._client.scan(cursor, match=self._dead_key("*"), count=100)
                keys.extend(found)
                if cursor == 0:
                    break
        for key in keys:
            items = await self._client.lrange(key, 0, limit - 1)
            if items:
                await self._client.ltrim(key, len(items), -1)
                drained.extend(self._deserialize_job(item) for item in items)
            if len(drained) >= limit:
                break
        return drained[:limit]

    async def _do_health_check(self) -> HealthComponent:
        if self._client:
            try:
                start = time_module.time()
                await self._client.ping()
                latency = time_module.time() - start
                return HealthComponent(
                    name=self.name,
                    status=HealthStatus.HEALTHY if latency < 0.5 else HealthStatus.DEGRADED,
                    metadata={
                        "redis_connected": True,
                        "latency_ms": latency * 1000,
                        "handlers": len(self._handlers),
                        "running": self._running,
                    },
                )
            except Exception as e:
                return HealthComponent(name=self.name, status=HealthStatus.DEGRADED, message=str(e))
        return HealthComponent(name=self.name, status=HealthStatus.DEGRADED, message="In-memory queue (single process)")


_queue_manager: QueueManager | None = None


def get_queue_manager() -> QueueManager:
    if _queue_manager is None:
        raise RuntimeError("Queue manager not initialized")
    return _queue_manager


def init_queue(redis_url: str = "redis://localhost:6379", **kwargs) -> QueueManager:
    global _queue_manager
    config = QueueConfig(redis_url=redis_url, **kwargs)
    _queue_manager = QueueManager(config)
    return _queue_manager
