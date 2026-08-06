"""Real cleanup operations: vector store retention, dead-letter sweep,
and stale file removal."""

import structlog
from datetime import datetime, timezone

from backend.app.adapters.vectorstore.provider import BaseVectorStore
from backend.app.infrastructure.queue.manager import QueueManager

logger = structlog.get_logger(__name__)


class CleanupService:
    def __init__(
        self,
        vector_store: BaseVectorStore,
        queue_manager: QueueManager,
        storage_manager=None,
    ):
        self.vector_store = vector_store
        self.queue_manager = queue_manager
        self.storage_manager = storage_manager

    async def cleanup_vectors(
        self,
        tenant_id: str | None = None,
        document_id: str | None = None,
        source: str | None = None,
        older_than_seconds: int | None = None,
    ) -> int:
        """Delete vectors by exact metadata match and/or age.

        Documents must carry ``stored_at`` (unix timestamp) metadata for
        age-based retention; documents without it are kept when an age
        cutoff is requested (fail-safe: never delete what cannot be dated).
        """
        filters = {}
        if tenant_id is not None:
            filters["tenant_id"] = tenant_id
        if document_id is not None:
            filters["document_id"] = document_id
        if source is not None:
            filters["source"] = source

        before = None
        if older_than_seconds is not None:
            if not filters:
                raise ValueError(
                    "age-based vector cleanup requires a metadata filter "
                    "(tenant_id, document_id or source) so unrelated data is never touched"
                )
            before = datetime.now(timezone.utc).timestamp() - older_than_seconds

        deleted = await self.vector_store.delete_matching(filters, before=before)
        logger.info(
            "vectors_cleaned",
            deleted=deleted,
            filters=filters,
            before=before,
        )
        return deleted

    async def sweep_dead_letter(
        self,
        job_type: str | None = None,
        limit: int = 100,
        requeue: bool = False,
    ) -> dict:
        """Drain the dead-letter queue.

        ``requeue=True`` re-enqueues drained jobs (idempotency keys are
        preserved, so already-processed payloads will be skipped by the
        worker). Returns a report of what was drained.
        """
        drained = await self.queue_manager.drain_dead_letter(job_type=job_type, limit=limit)
        requeued = 0
        for job in drained:
            if requeue and job.type:
                job.retries = 0
                job.error = None
                if await self.queue_manager.enqueue(job):
                    requeued += 1
        report = {
            "drained": len(drained),
            "requeued": requeued,
            "job_types": sorted({job.type for job in drained if job.type}),
        }
        logger.info("dead_letter_swept", **report)
        return report

    async def cleanup_storage(self, prefix: str = "", older_than_seconds: int | None = None) -> int:
        """Delete stale files from the storage backend."""
        if not self.storage_manager:
            raise ValueError("storage_manager is required for storage cleanup")
        files = await self.storage_manager.list_files(prefix=prefix)
        cutoff = (
            datetime.now(timezone.utc).timestamp() - older_than_seconds
            if older_than_seconds is not None
            else None
        )
        deleted = 0
        for metadata in files:
            created = metadata.created_at
            if created is not None and cutoff is not None and created.timestamp() >= cutoff:
                continue
            if await self.storage_manager.delete_file(metadata.key):
                deleted += 1
        logger.info("storage_cleaned", prefix=prefix, deleted=deleted)
        return deleted
