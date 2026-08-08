"""P1-7 — Cold-tier thread archive service.

Threads that outlive the tenant's retention window are archived to
region-pinned object storage: the full durable payload (thread row,
messages, parts, events) is serialized to ``tenant/{id}/archive/{region}/``
and the thread is marked ``archived`` in the DB — hot queries and the
retention sweep then ignore it. Restore pulls the payload back for
verification and flips the marker off. The DB remains the only source of
truth; the object payload is the cold-tier copy (ties P6-8 retention).

Archive and restore are idempotent: re-archiving an archived thread is a
no-op that returns the existing state, and a failed upload never flips
the marker (the hot copy stays authoritative).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import structlog

from backend.app.governance.residency import archive_prefix
from backend.app.infrastructure.db.threads import ThreadNotFoundError

logger = structlog.get_logger(__name__)

ARCHIVE_MEDIA_TYPE = "application/json"

THREAD_ARCHIVE_SUFFIX = "threads"


class ThreadAlreadyArchivedError(Exception):
    pass


class ThreadArchiveService:
    """Move threads between the hot tier and the cold-tier object store."""

    def __init__(self, db, storage):
        self.db = db
        self.storage = storage

    # ------------------------------------------------------------------
    # keys
    # ------------------------------------------------------------------
    def archive_key(
        self, tenant_id: str, thread_id: str, region: str | None = None
    ) -> str:
        return f"{archive_prefix(tenant_id, region)}{THREAD_ARCHIVE_SUFFIX}/{thread_id}.json"

    # ------------------------------------------------------------------
    # archive
    # ------------------------------------------------------------------
    async def archive_thread(
        self,
        tenant_id: str,
        thread_id: str,
        *,
        region: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Archive one thread: dump -> upload -> mark archived.

        The marker is flipped only after the upload succeeds (verified by
        checksum in metadata). Idempotent: an already-archived thread
        returns the current state without a second upload.
        """
        from backend.app.infrastructure.db import ThreadRepository

        repo = ThreadRepository(self.db)
        thread = await repo.get_thread(tenant_id, thread_id)
        if thread is None:
            raise ThreadNotFoundError(f"Thread not found: {thread_id}")
        if thread.get("archived"):
            return {
                "thread_id": thread_id,
                "archived": True,
                "already_archived": True,
                "key": self.archive_key(tenant_id, thread_id, region),
            }

        payload = await repo.dump_thread(tenant_id, thread_id)
        if payload is None:
            raise ThreadNotFoundError(f"Thread not found: {thread_id}")

        body = json.dumps(payload, default=str).encode("utf-8")
        checksum = hashlib.sha256(body).hexdigest()
        key = self.archive_key(tenant_id, thread_id, region)
        await self.storage.upload_file(
            key,
            body,
            content_type=ARCHIVE_MEDIA_TYPE,
            metadata={
                "tenant_id": tenant_id,
                "thread_id": thread_id,
                "checksum_sha256": checksum,
                "region": region or "",
            },
        )

        result = await repo.set_archived(
            tenant_id, thread_id, True, request_id=request_id
        )
        logger.info(
            "thread_archived",
            tenant_id=tenant_id,
            thread_id=thread_id,
            key=key,
            bytes=len(body),
            checksum=checksum,
        )
        return {
            "thread_id": thread_id,
            "archived": True,
            "key": key,
            "bytes": len(body),
            "checksum_sha256": checksum,
            "event": result["event"],
        }

    async def archive_tenant_window(
        self,
        tenant_id: str,
        *,
        older_than_days: int,
        limit: int = 50,
        region: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Sweep a tenant's threads older than the retention window (P6-8).

        Returns the per-thread archive results; upload failures are
        recorded and do not abort the sweep (the offending thread simply
        stays hot and is retried on the next run).
        """
        from backend.app.infrastructure.db import ThreadRepository

        repo = ThreadRepository(self.db)
        candidates = await repo.list_threads_archivable(
            tenant_id, older_than_days=older_than_days, limit=limit
        )
        archived: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        for thread in candidates:
            try:
                archived.append(
                    await self.archive_thread(
                        tenant_id,
                        str(thread["id"]),
                        region=region,
                        request_id=request_id,
                    )
                )
            except Exception as e:  # pragma: no cover - defensive sweep
                logger.warning(
                    "thread_archive_failed",
                    tenant_id=tenant_id,
                    thread_id=str(thread["id"]),
                    error=str(e),
                )
                failed.append({"thread_id": str(thread["id"]), "error": str(e)})
        return {"archived": archived, "failed": failed}

    # ------------------------------------------------------------------
    # restore
    # ------------------------------------------------------------------
    async def restore_thread(
        self,
        tenant_id: str,
        thread_id: str,
        *,
        region: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Restore an archived thread: pull payload, verify, unmark.

        The object payload is downloaded and its checksum verified before
        the archived flag is cleared — a corrupt cold-tier copy cannot
        silently re-enable the thread. Idempotent: a hot thread returns
        the current state untouched.
        """
        from backend.app.infrastructure.db import ThreadRepository

        repo = ThreadRepository(self.db)
        thread = await repo.get_thread(tenant_id, thread_id)
        if thread is None:
            raise ThreadNotFoundError(f"Thread not found: {thread_id}")
        if not thread.get("archived"):
            return {
                "thread_id": thread_id,
                "archived": False,
                "already_restored": True,
            }

        key = self.archive_key(tenant_id, thread_id, region)
        data, metadata = await self.storage.download_file(key)
        checksum = hashlib.sha256(data).hexdigest()
        expected = metadata.metadata.get("checksum_sha256") if metadata.metadata else None
        if expected and checksum != expected:
            raise ValueError(
                f"archive checksum mismatch for {thread_id}: "
                f"expected {expected}, got {checksum}"
            )
        payload = json.loads(data.decode("utf-8"))
        if str(payload.get("thread", {}).get("id")) != thread_id:
            raise ValueError(
                f"archive payload thread id mismatch for {key}"
            )

        result = await repo.set_archived(
            tenant_id, thread_id, False, request_id=request_id
        )
        logger.info(
            "thread_restored",
            tenant_id=tenant_id,
            thread_id=thread_id,
            key=key,
            messages=len(payload.get("messages", [])),
        )
        return {
            "thread_id": thread_id,
            "archived": False,
            "key": key,
            "messages": len(payload.get("messages", [])),
            "events": len(payload.get("events", [])),
            "checksum_sha256": checksum,
            "event": result["event"],
        }
