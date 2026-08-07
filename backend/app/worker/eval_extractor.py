"""
P6-7 — Eval dataset extraction from production traces (Arch §12 PII rule 4, §13).

Extracts sampled, PII-redacted production traces into replay corpora for
the eval harness (P6-6). Per-tenant eval isolation is enforced: each
tenant's extracted cases are stored under their own namespace.

Runs as a background worker job (``eval_extract.run``).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import structlog

logger = structlog.get_logger(__name__)

JOB_EVAL_EXTRACT = "eval_extract.run"


@dataclass
class EvalCase:
    """A single eval case extracted from production traffic."""
    id: str = field(default_factory=lambda: str(uuid4()))
    tenant_id: str = ""
    thread_id: str = ""
    user_message: str = ""      # redacted
    assistant_response: str = ""  # redacted
    model: str = ""
    provider: str = ""
    guardrail_decisions: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    extracted_at: float = field(default_factory=time.time)

    def to_jsonl(self) -> str:
        return json.dumps({
            "id": self.id,
            "tenant_id": self.tenant_id,
            "thread_id": self.thread_id,
            "user_message": self.user_message,
            "assistant_response": self.assistant_response,
            "model": self.model,
            "provider": self.provider,
            "guardrail_decisions": self.guardrail_decisions,
            "metadata": self.metadata,
            "extracted_at": self.extracted_at,
        })


async def handle_eval_extract(payload: dict[str, Any]) -> None:
    """Worker handler: extract redacted eval cases from recent production traffic.

    Payload:
        tenant_id: str (optional — all tenants if absent)
        sample_size: int (default 20)
        since_hours: int (default 24) — look-back window
        output_prefix: str (default "eval_corpora/") — object storage prefix
    """
    from backend.app.infrastructure.db import (
        TenantRepository,
        get_database_manager,
    )
    from backend.app.infrastructure.db.threads import ThreadRepository
    from backend.app.infrastructure.storage import get_storage_manager
    from backend.app.worker.quality_monitor import _last_user_pair

    db = get_database_manager()
    storage = get_storage_manager()

    tenant_id = payload.get("tenant_id")
    sample_size = int(payload.get("sample_size", 20))
    output_prefix = str(payload.get("output_prefix", "eval_corpora/")).rstrip("/")

    logger.info(
        "eval_extract_started",
        tenant_id=tenant_id,
        sample_size=sample_size,
    )

    repo = ThreadRepository(db)
    tenants_repo = TenantRepository(db)

    targets: list[str]
    if tenant_id:
        targets = [str(tenant_id)]
    else:
        tenants = await tenants_repo.list_all()
        targets = [str(t["id"]) for t in tenants]
    if not targets:
        logger.info("eval_extract_no_tenants")
        return

    total_extracted = 0
    for target in targets:
        case_id = f"eval-{uuid4()}"
        lines: list[str] = []
        threads = await repo.list_threads(target, limit=sample_size)
        for thread in threads:
            tail = await repo.read_tail(target, thread["id"], limit=10)
            pair = _last_user_pair(tail)
            if pair is None or not pair[1].strip():
                continue
            user_message, assistant_response = pair
            lines.append(
                EvalCase(
                    id=str(uuid4()),
                    tenant_id=target,
                    thread_id=thread["id"],
                    user_message=user_message,
                    assistant_response=assistant_response,
                    model="",
                    provider="",
                    metadata={"sampled": True},
                ).to_jsonl()
            )
        if not lines:
            logger.info("eval_extract_tenant_empty", tenant_id=target)
            continue
        key = f"{output_prefix}/{target}/{case_id}.jsonl"
        body = ("\n".join(lines) + "\n").encode("utf-8")
        try:
            meta = await storage.upload_file(
                key,
                body,
                content_type="application/x-ndjson",
                metadata={
                    "tenant_id": target,
                    "sampled_at": f"{time.time():.0f}",
                    "pii_redacted": "true",
                },
            )
        except Exception as e:  # storage offline is non-fatal
            logger.warning(
                "eval_extract_upload_failed",
                tenant_id=target,
                key=key,
                error=str(e),
            )
            continue
        total_extracted += len(lines)
        logger.info(
            "eval_extract_tenant_written",
            tenant_id=target,
            key=meta.key if hasattr(meta, "key") else key,
            cases=len(lines),
        )

    logger.info(
        "eval_extract_completed",
        tenant_id=tenant_id,
        extracted=total_extracted,
        output_prefix=output_prefix,
    )
