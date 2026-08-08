"""
P6-8 — Retention and archive automation (Arch §11, §14).

Automates tenant-configurable data retention for threads, traces, logs,
and evidence. Supports GDPR export/erase automation (ties P5-10).

Runs as a background worker job (``retention.run``), typically scheduled
daily via the worker schedule file.

Retention policy:
  - Each tenant has a configurable retention window (default 90 days).
  - Threads older than the window are archived to object storage (ties P1-7).
  - Traces, spend events, and audit logs older than the evidence retention
    window are purged (evidence retention may be longer per compliance).
  - GDPR erasure requests queue immediate deletion for a specific end user.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

JOB_RETENTION_RUN = "retention.run"
JOB_THREAD_ARCHIVE = "thread.archive"
JOB_THREAD_RESTORE = "thread.restore"
JOB_GDPR_ERASE = "gdpr.erase"
JOB_GDPR_EXPORT = "gdpr.export"


@dataclass
class RetentionPolicy:
    """Per-tenant retention configuration."""
    tenant_id: str
    thread_retention_days: int = 90       # archive after this many days
    trace_retention_days: int = 90        # purge traces
    spend_retention_days: int = 365       # keep spend records longer
    evidence_retention_days: int = 730    # compliance: 2 years minimum
    audit_retention_days: int = 730       # compliance: 2 years minimum


DEFAULT_RETENTION = RetentionPolicy(tenant_id="__default__")


async def handle_retention_run(payload: dict[str, Any]) -> None:
    """Worker handler: run the retention sweep.

    Payload:
        tenant_id: str (optional — all tenants if absent)
        dry_run: bool (default False — log what would be deleted)
    """
    from datetime import datetime, timedelta

    from backend.app.infrastructure.db import (
        SessionTokenRepository,
        TenantRepository,
        get_database_manager,
    )

    db = get_database_manager()

    tenant_id = payload.get("tenant_id")
    dry_run = payload.get("dry_run", False)

    logger.info(
        "retention_sweep_started",
        tenant_id=tenant_id or "all",
        dry_run=dry_run,
    )

    tenants_repo = TenantRepository(db)

    targets: list[str]
    if tenant_id:
        targets = [str(tenant_id)]
    else:
        tenants = await tenants_repo.list_all()
        targets = [str(t["id"]) for t in tenants]
    if not targets:
        logger.info("retention_sweep_no_tenants")
        return

    # Phase 1: thread archival (P1-7) — threads older than the retention
    # window move to the cold-tier object store.
    from backend.app.application.archive.service import ThreadArchiveService
    from backend.app.governance.residency import resolve_region
    from backend.app.infrastructure.storage import get_storage_manager

    storage = get_storage_manager()
    archive_service = ThreadArchiveService(db, storage)
    archived_threads = 0
    # Phase 2-3 counts for the summary.
    purged_spend = 0
    purged_evidence = 0
    purged_audit = 0
    purged_traces = 0

    policy = DEFAULT_RETENTION
    tokens = SessionTokenRepository(db)
    cutoff = datetime.now(UTC) - timedelta(days=policy.audit_retention_days)
    purged_tokens = 0
    for target in targets:
        tenant_row = await tenants_repo.get_by_id(target)
        # Per-tenant thread window: `tenants.retention_days` overrides the
        # default posture (P5-9); other windows keep the defaults.
        thread_window = policy.thread_retention_days
        if tenant_row and tenant_row.get("retention_days"):
            thread_window = int(tenant_row["retention_days"])
        if not dry_run:
            try:
                region = None
                if tenant_row:
                    region = resolve_region(tenant_row.get("region"))
                sweep = await archive_service.archive_tenant_window(
                    target,
                    older_than_days=thread_window,
                    limit=100,
                    region=region,
                    request_id=f"retention:{target}",
                )
                archived_threads += len(sweep["archived"])
            except Exception as e:  # pragma: no cover - defensive
                logger.warning(
                    "retention_thread_archive_failed",
                    tenant_id=target,
                    error=str(e),
                )
        # Session tokens: delete anything expired/revoked past the window.
        try:
            if not dry_run:
                purged_tokens += await tokens.prune_expired(target, before=cutoff)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("retention_token_prune_failed", tenant_id=target, error=str(e))
        # Phase 2: spend events past the spend window (traces ride on spend —
        # the trace view is derived from spend events, so trace expiry is the
        # spend window; P7-4).
        try:
            if not dry_run:
                from backend.app.infrastructure.db import SpendEventRepository

                spend_cutoff = datetime.now(UTC) - timedelta(
                    days=policy.spend_retention_days
                )
                purged_spend += await SpendEventRepository(db).purge_before(
                    target, spend_cutoff
                )
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("retention_spend_purge_failed", tenant_id=target, error=str(e))
        # Phase 3: evidence packets past the evidence window (compliance 2y).
        try:
            if not dry_run:
                from backend.app.infrastructure.db import EvidenceRepository

                evidence_cutoff = datetime.now(UTC) - timedelta(
                    days=policy.evidence_retention_days
                )
                purged_evidence += await EvidenceRepository(db).purge_before(
                    target, evidence_cutoff
                )
        except Exception as e:  # pragma: no cover - defensive
            logger.warning(
                "retention_evidence_purge_failed", tenant_id=target, error=str(e)
            )
        logger.info(
            "retention_tenant_sweep",
            tenant_id=target,
            dry_run=dry_run,
            thread_window_days=thread_window,
            purged_tokens=purged_tokens,
        )

    logger.info(
        "retention_sweep_completed",
        tenant_id=tenant_id or "all",
        dry_run=dry_run,
        archived_threads=archived_threads,
        purged_traces=purged_traces,
        purged_spend=purged_spend,
        purged_evidence=purged_evidence,
        purged_audit=purged_audit,
        purged_tokens=purged_tokens,
        audit_retained_by_design=True,
    )


async def handle_thread_archive(payload: dict[str, Any]) -> None:
    """Worker handler: archive a single thread to the cold tier (P1-7).

    Payload:
        tenant_id: str (required)
        thread_id: str (required)
        region: str (optional — tenant's configured region)
        request_id: str (optional idempotency key)
    """
    tenant_id = payload.get("tenant_id")
    thread_id = payload.get("thread_id")
    if not tenant_id or not thread_id:
        raise ValueError("thread.archive requires tenant_id and thread_id")

    logger.info(
        "thread_archive_job_started",
        tenant_id=tenant_id,
        thread_id=thread_id,
    )

    from backend.app.application.archive.service import ThreadArchiveService
    from backend.app.infrastructure.db import get_database_manager
    from backend.app.infrastructure.storage import get_storage_manager

    service = ThreadArchiveService(get_database_manager(), get_storage_manager())
    try:
        result = await service.archive_thread(
            str(tenant_id),
            str(thread_id),
            region=payload.get("region"),
            request_id=payload.get("request_id"),
        )
    except Exception as e:
        logger.warning(
            "thread_archive_job_failed",
            tenant_id=tenant_id,
            thread_id=thread_id,
            error=str(e),
        )
        raise

    logger.info(
        "thread_archive_job_completed",
        tenant_id=tenant_id,
        thread_id=thread_id,
        key=result.get("key"),
        bytes=result.get("bytes"),
        already_archived=result.get("already_archived", False),
    )


async def handle_thread_restore(payload: dict[str, Any]) -> None:
    """Worker handler: restore a single thread from the cold tier (P1-7).

    Payload:
        tenant_id: str (required)
        thread_id: str (required)
        region: str (optional — tenant's configured region)
        request_id: str (optional idempotency key)
    """
    tenant_id = payload.get("tenant_id")
    thread_id = payload.get("thread_id")
    if not tenant_id or not thread_id:
        raise ValueError("thread.restore requires tenant_id and thread_id")

    logger.info(
        "thread_restore_job_started",
        tenant_id=tenant_id,
        thread_id=thread_id,
    )

    from backend.app.application.archive.service import ThreadArchiveService
    from backend.app.infrastructure.db import get_database_manager
    from backend.app.infrastructure.storage import get_storage_manager

    service = ThreadArchiveService(get_database_manager(), get_storage_manager())
    try:
        result = await service.restore_thread(
            str(tenant_id),
            str(thread_id),
            region=payload.get("region"),
            request_id=payload.get("request_id"),
        )
    except Exception as e:
        logger.warning(
            "thread_restore_job_failed",
            tenant_id=tenant_id,
            thread_id=thread_id,
            error=str(e),
        )
        raise

    logger.info(
        "thread_restore_job_completed",
        tenant_id=tenant_id,
        thread_id=thread_id,
        key=result.get("key"),
        messages=result.get("messages"),
        already_restored=result.get("already_restored", False),
    )


async def handle_gdpr_erase(payload: dict[str, Any]) -> None:
    """Worker handler: GDPR data subject erasure request.

    Erases all data for a specific end user within a tenant. Delegates to
    ``TenantLifecycleService.erase_end_user_data`` (the same code path the
    synchronous DSR API uses) so the worker and the API never diverge.

    Payload:
        tenant_id: str (required)
        end_user_id: str (required)
        request_id: str (DSR tracking ID)
    """
    tenant_id = payload.get("tenant_id")
    end_user_id = payload.get("end_user_id")
    request_id = payload.get("request_id", "")

    if not tenant_id or not end_user_id:
        raise ValueError("gdpr.erase requires tenant_id and end_user_id")

    logger.info(
        "gdpr_erase_started",
        tenant_id=tenant_id,
        end_user_id=end_user_id,
        request_id=request_id,
    )

    from backend.app.application.tenant_lifecycle import TenantLifecycleService

    try:
        deleted = await TenantLifecycleService().erase_end_user_data(
            str(tenant_id), str(end_user_id)
        )
    except ValueError as e:
        logger.warning("gdpr_erase_end_user_missing", error=str(e))
        raise

    logger.info(
        "gdpr_erase_completed",
        tenant_id=tenant_id,
        end_user_id=end_user_id,
        request_id=request_id,
        **deleted,
    )


async def handle_gdpr_export(payload: dict[str, Any]) -> None:
    """Worker handler: GDPR data subject export request.

    Exports all data for a specific end user and stores the sequence JSON
    bundle under tenant:{id}/exports/{request_id}.json so it survives the
    request lifetime. Delegates the bundle build to the same service used
    by the sync DSR endpoint.

    Payload:
        tenant_id: str (required)
        end_user_id: str (required)
        request_id: str (DSR tracking ID)
    """
    tenant_id = payload.get("tenant_id")
    end_user_id = payload.get("end_user_id")
    request_id = payload.get("request_id", "")

    if not tenant_id or not end_user_id:
        raise ValueError("gdpr.export requires tenant_id and end_user_id")

    logger.info(
        "gdpr_export_started",
        tenant_id=tenant_id,
        end_user_id=end_user_id,
        request_id=request_id,
    )

    from backend.app.application.tenant_lifecycle import TenantLifecycleService
    from backend.app.infrastructure.storage import get_storage_manager

    service = TenantLifecycleService()
    try:
        bundle = await service.export_end_user_data(
            str(tenant_id), str(end_user_id)
        )
    except ValueError as e:
        logger.warning("gdpr_export_user_missing", error=str(e))
        raise

    storage = get_storage_manager()
    key = f"tenant:{tenant_id}/exports/{request_id}.json"
    try:
        import json as _json

        await storage.upload_file(
            key,
            _json.dumps(bundle, default=str).encode("utf-8"),
            content_type="application/json",
            metadata={
                "tenant_id": str(tenant_id),
                "end_user_id": str(end_user_id),
                "request_id": request_id,
            },
        )
    except Exception as e:
        logger.warning("gdpr_export_upload_failed", key=key, error=str(e))
        raise

    logger.info("gdpr_export_completed", tenant_id=tenant_id, key=key, request_id=request_id)
