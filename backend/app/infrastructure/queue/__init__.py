"""
Queue infrastructure layer.

Provides Redis-based message queue for background jobs and async processing.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable
from uuid import uuid4

import structlog

logger = structlog.get_logger(__name__)


@dataclass
class Job:
    """Represents a queued job."""

    id: str = field(default_factory=lambda: str(uuid4()))
    type: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    priority: int = 0
    created_at: datetime = field(default_factory=datetime.utcnow)
    retries: int = 0
    max_retries: int = 3
    error: str | None = None


class QueueManager:
    """Manages Redis-based job queue."""

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379",
        prefix: str = "neryva:queue:",
    ):
        self.redis_url = redis_url
        self.prefix = prefix
        self._client: Any | None = None
        self._handlers: dict[str, Callable] = {}

    async def initialize(self) -> None:
        """Initialize Redis connection."""
        try:
            import redis.asyncio as redis

            self._client = redis.from_url(
                self.redis_url,
                decode_responses=True,
            )
            await self._client.ping()
            logger.info("queue_initialized", url=self.redis_url)
        except ImportError:
            logger.warning("redis_not_available", message="Redis client not installed")
        except Exception as e:
            logger.error("queue_init_error", error=str(e))

    async def close(self) -> None:
        """Close Redis connection."""
        if self._client:
            await self._client.close()
            self._client = None
            logger.info("queue_closed")

    def register_handler(self, job_type: str, handler: Callable) -> None:
        """Register a handler for a job type."""
        self._handlers[job_type] = handler
        logger.info("handler_registered", job_type=job_type)

    def _queue_key(self, priority: int = 0) -> str:
        """Generate queue key based on priority."""
        if priority > 0:
            return f"{self.prefix}high"
        elif priority < 0:
            return f"{self.prefix}low"
        return f"{self.prefix}default"

    async def enqueue(self, job: Job) -> bool:
        """Add a job to the queue."""
        if self._client is None:
            return False

        try:
            serialized = json.dumps({
                "id": job.id,
                "type": job.type,
                "payload": job.payload,
                "priority": job.priority,
                "created_at": job.created_at.isoformat(),
                "retries": job.retries,
                "max_retries": job.max_retries,
            })
            queue_key = self._queue_key(job.priority)
            await self._client.lpush(queue_key, serialized)
            logger.info("job_enqueued", job_id=job.id, job_type=job.type)
            return True
        except Exception as e:
            logger.error("enqueue_error", job_id=job.id, error=str(e))
            return False

    async def dequeue(self, timeout: int = 1) -> Job | None:
        """Get a job from the queue."""
        if self._client is None:
            return None

        try:
            # Check high priority first, then default, then low
            for queue_key in [f"{self.prefix}high", f"{self.prefix}default", f"{self.prefix}low"]:
                result = await self._client.brpop(queue_key, timeout=timeout)
                if result:
                    _, serialized = result
                    data = json.loads(serialized)
                    job = Job(
                        id=data["id"],
                        type=data["type"],
                        payload=data["payload"],
                        priority=data.get("priority", 0),
                        created_at=datetime.fromisoformat(data["created_at"]),
                        retries=data.get("retries", 0),
                        max_retries=data.get("max_retries", 3),
                    )
                    logger.info("job_dequeued", job_id=job.id, job_type=job.type)
                    return job
            return None
        except Exception as e:
            logger.error("dequeue_error", error=str(e))
            return None

    async def process_job(self, job: Job) -> bool:
        """Process a job using the registered handler."""
        handler = self._handlers.get(job.type)
        if not handler:
            logger.warning("no_handler_for_job", job_type=job.type)
            return False

        try:
            await handler(job.payload)
            logger.info("job_processed", job_id=job.id, job_type=job.type)
            return True
        except Exception as e:
            logger.error("job_processing_error", job_id=job.id, error=str(e))
            job.error = str(e)
            job.retries += 1

            # Requeue if retries remain
            if job.retries < job.max_retries:
                logger.info("job_requeued", job_id=job.id, retry=job.retries)
                await self.enqueue(job)
            else:
                logger.error("job_failed_permanently", job_id=job.id)

            return False

    async def health_check(self) -> bool:
        """Check queue connectivity."""
        if self._client is None:
            return False

        try:
            await self._client.ping()
            return True
        except Exception as e:
            logger.error("queue_health_check_failed", error=str(e))
            return False


# Global queue instance
_queue_manager: QueueManager | None = None


def get_queue_manager() -> QueueManager:
    """Get the global queue manager instance."""
    if _queue_manager is None:
        raise RuntimeError("Queue manager not initialized")
    return _queue_manager


def init_queue(redis_url: str = "redis://localhost:6379", **kwargs) -> QueueManager:
    """Initialize the global queue manager."""
    global _queue_manager
    _queue_manager = QueueManager(redis_url, **kwargs)
    return _queue_manager