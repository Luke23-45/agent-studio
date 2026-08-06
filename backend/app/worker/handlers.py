"""Worker job handlers.

One handler per job type. Handlers raise on failure so the queue layer
can retry with backoff and dead-letter permanently failed jobs; they
never silently swallow errors.
"""

import structlog
from typing import Any, Callable, Dict

from backend.app.infrastructure.queue.manager import Job, get_queue_manager

logger = structlog.get_logger(__name__)

JOB_INGESTION_PROCESS = "ingestion.process"
JOB_INGESTION_EMBED = "ingestion.embed"
JOB_NOTIFICATION_SEND = "notification.send"
JOB_CLEANUP_RUN = "cleanup.run"
JOB_EVAL_REPLAY = "eval.replay"
JOB_REDTEAM_RUN = "redteam.run"
JOB_WEBHOOK_DELIVER = "webhook.deliver"


async def handle_ingestion_process(payload: dict[str, Any]) -> None:
    """Chunk a document with the real ingestion pipeline."""
    from uuid import UUID

    from backend.app.application.ingestion import create_ingestion_service

    content = payload.get("content")
    source = payload.get("source", "")
    if not content:
        raise ValueError("ingestion.process requires payload['content']")

    service = create_ingestion_service()
    job = await service.ingest_document(
        content=content,
        source=source,
        tenant_id=UUID(payload["tenant_id"]) if payload.get("tenant_id") else None,
        document_id=payload.get("document_id"),
        metadata=payload.get("metadata") or {},
    )
    if job.chunks:
        manager = get_queue_manager()
        follow_up = Job(
            type=JOB_INGESTION_EMBED,
            payload={
                "chunks": job.chunks,
                "tenant_id": str(job.tenant_id) if job.tenant_id else payload.get("tenant_id"),
                "document_id": job.document_id,
                "source": job.source,
                "embedding_dim": payload.get("embedding_dim"),
            },
        )
        idem_key = payload.get("_idem")
        embed_key = f"embed:{job.document_id}" + (f":{idem_key}" if idem_key else "")
        await manager.enqueue(follow_up, idempotency_key=embed_key)
        logger.info("job_embed_enqueued", document_id=job.document_id, chunks=len(job.chunks))
    logger.info(
        "job_ingestion_done",
        job_id=job.id,
        document_id=job.document_id,
        chunks=len(job.chunks),
    )


async def handle_ingestion_embed(payload: dict[str, Any]) -> None:
    """Embed chunks and index them into the configured vector store."""
    from backend.app.adapters.vectorstore import create_vector_store_from_settings
    from backend.app.application.ingestion.embeddings import embed_and_index

    chunks = payload.get("chunks")
    if not isinstance(chunks, list) or not chunks:
        raise ValueError("ingestion.embed requires payload['chunks']")
    tenant_id = payload.get("tenant_id")
    document_id = payload.get("document_id")
    source = payload.get("source", "")
    if not tenant_id or not document_id:
        raise ValueError("ingestion.embed requires payload['tenant_id'] and payload['document_id']")

    vector_store = create_vector_store_from_settings()
    embedding_dim = int(payload.get("embedding_dim") or getattr(vector_store, "embedding_dim", 384))
    ids = await embed_and_index(
        chunks=chunks,
        vector_store=vector_store,
        tenant_id=tenant_id,
        document_id=document_id,
        source=source,
        embedding_dim=embedding_dim,
    )
    logger.info("job_embed_done", document_id=document_id, stored=len(ids))


async def handle_notification_send(payload: dict[str, Any]) -> None:
    """Deliver a notification over a real transport (webhook / SMTP / SMS)."""
    from backend.app.modules.notifications import (
        NotificationChannel,
        NotificationRequest,
        create_notification_service,
    )

    channel = NotificationChannel(payload.get("channel", "webhook"))
    request = NotificationRequest(
        tenant_id=payload.get("tenant_id", ""),
        session_id=payload.get("session_id", ""),
        channel=channel,
        to=payload.get("to", ""),
        subject=payload.get("subject", ""),
        body=payload.get("body", ""),
        metadata=payload.get("metadata") or {},
    )
    service = create_notification_service()
    result = await service.send(request)
    if not result.success:
        raise ValueError(f"notification delivery failed: {result.error}")
    logger.info("job_notification_done", id=request.id, channel=channel.value)


async def handle_cleanup_run(payload: dict[str, Any]) -> None:
    """Run retention cleanup: vector store + dead-letter sweep."""
    from backend.app.adapters.vectorstore import create_vector_store_from_settings
    from backend.app.application.cleanup.service import CleanupService

    vector_store = create_vector_store_from_settings()
    service = CleanupService(
        vector_store=vector_store,
        queue_manager=get_queue_manager(),
    )
    deleted = 0
    filters = {
        key: payload.get(key)
        for key in ("tenant_id", "document_id", "source")
        if payload.get(key) is not None
    }
    if filters or payload.get("older_than_seconds") is not None:
        deleted = await service.cleanup_vectors(
            tenant_id=payload.get("tenant_id"),
            document_id=payload.get("document_id"),
            source=payload.get("source"),
            older_than_seconds=payload.get("older_than_seconds"),
        )
    else:
        logger.info("job_cleanup_vectors_skipped", reason="no metadata filters")
    sweep = await service.sweep_dead_letter(
        job_type=payload.get("job_type"),
        limit=int(payload.get("limit", 100)),
        requeue=bool(payload.get("requeue_dead_letter", False)),
    )
    logger.info("job_cleanup_done", deleted=deleted, sweep=sweep)


async def handle_eval_replay(payload: dict[str, Any]) -> None:
    """Replay a stored conversation through the real pipeline."""
    from backend.app.application.eval_replay.service import EvalReplayService

    tenant_id = payload.get("tenant_id")
    session_id = payload.get("session_id")
    if not tenant_id or not session_id:
        raise ValueError("eval.replay requires payload['tenant_id'] and payload['session_id']")

    from backend.app.infrastructure.db import get_database_manager

    service = EvalReplayService(db=get_database_manager())
    report = await service.replay_conversation(
        tenant_id=str(tenant_id),
        session_id=str(session_id),
        ticketing_webhook_url=payload.get("ticketing_webhook_url"),
    )
    logger.info("job_eval_replay_done", **{k: v for k, v in report.items() if k != "errors"})


async def handle_redteam_run(payload: dict[str, Any]) -> None:
    """Run the deterministic attack suites against the guardrail pipeline.

    Findings are reported (and audited), not raised: a scan that finds
    gaps is a result that must reach the review queue. Only pipeline
    errors propagate for retry/DLQ handling.
    """
    from backend.app.application.redteam import create_red_team_runner

    runner = create_red_team_runner()
    report = await runner.run_all()
    report_dict = report.to_dict()
    summary = report_dict["summary"]
    logger.info(
        "job_redteam_done",
        run_id=report.run_id,
        cases=summary["cases"],
        blocked=summary["blocked"],
        failed=summary["failed"],
    )

    try:
        from backend.app.infrastructure.db import get_database_manager
        from backend.app.infrastructure.db.repositories import AuditRepository

        db = get_database_manager()
        await AuditRepository(db).add(
            action="redteam.run",
            resource_type="redteam",
            resource_id=report.run_id,
            tenant_id=payload.get("tenant_id"),
            actor_type="system",
            details={
                "runner_version": report_dict["runner_version"],
                "summary": summary,
                "failures": report_dict["failures"][:20],
            },
        )
    except Exception as e:  # audit must never fail the scan job
        logger.warning("redteam_audit_failed", error=str(e))


async def handle_webhook_deliver(payload: dict[str, Any]) -> None:
    """Deliver a stored webhook event to one subscription (signed, 1.7)."""
    from backend.app.infrastructure.db import get_database_manager
    from backend.app.modules.webhooks import WebhookDeliverer, WebhookEnvelope
    from backend.app.infrastructure.db.repositories import WebhookRepository

    event_id = payload.get("event_id")
    subscription_id = payload.get("subscription_id")
    tenant_id = payload.get("tenant_id")
    if not event_id or not subscription_id:
        raise ValueError("webhook.deliver requires payload['event_id'] and payload['subscription_id']")

    repository = WebhookRepository(get_database_manager())
    event = await repository.get_event(event_id)
    if event is None:
        raise ValueError(f"webhook event not found: {event_id}")
    subscription = await repository.get_subscription(subscription_id, tenant_id)
    if subscription is None:
        raise ValueError(f"webhook subscription not found: {subscription_id}")
    if not subscription["active"]:
        logger.info("webhook_subscription_inactive", event_id=event_id, subscription_id=subscription_id)
        return

    envelope = WebhookEnvelope(
        event_id=event_id,
        event_type=event["event_type"],
        tenant_id=tenant_id,
        data=event["payload"],
    )
    result = await WebhookDeliverer().deliver(subscription["url"], subscription["secret"], envelope)
    await repository.record_delivery(
        event_id=event_id,
        subscription_id=subscription_id,
        tenant_id=tenant_id,
        status="success" if result.success else "failed",
        http_status=result.http_status,
        error=result.error,
    )
    if result.success:
        await repository.mark_delivered(event_id, subscription_id)

    if not result.success:
        raise ValueError(result.error or "webhook delivery failed")
    logger.info("job_webhook_delivered", event_id=event_id, subscription_id=subscription_id)


def build_handlers() -> Dict[str, Callable]:
    """Register every job handler with the shared queue manager."""
    handlers: Dict[str, Callable] = {
        JOB_INGESTION_PROCESS: handle_ingestion_process,
        JOB_INGESTION_EMBED: handle_ingestion_embed,
        JOB_NOTIFICATION_SEND: handle_notification_send,
        JOB_CLEANUP_RUN: handle_cleanup_run,
        JOB_EVAL_REPLAY: handle_eval_replay,
        JOB_REDTEAM_RUN: handle_redteam_run,
        JOB_WEBHOOK_DELIVER: handle_webhook_deliver,
    }
    manager = get_queue_manager()
    for job_type, handler in handlers.items():
        manager.register_handler(job_type, handler)
    return handlers
