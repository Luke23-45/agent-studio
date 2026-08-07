"""
Background summary refresh (Arch 8.2, P2-5).

The background worker keeps running summaries current so a triggered
compaction is an instant swap of an already-fresh checkpoint — never a
user-facing wait. Idempotent per thread: refresh skips threads whose
boundary has not outgrown ``min_growth_turns``, and the atomic swap is the
same P2-3 ``CompactionService.compact`` path (failures leave the prior
checkpoint active).
"""

from __future__ import annotations

import structlog
from dataclasses import dataclass
from typing import Any, Callable

logger = structlog.get_logger(__name__)

JOB_SUMMARY_REFRESH = "summary.refresh"

# Triggered preemptive compaction defers to this job so the turn keeps
# moving; the worker performs the actual summarization off the request path.
DEFAULT_MAX_THREADS_PER_RUN = 50
DEFAULT_MIN_GROWTH_TURNS = 10


@dataclass
class CompactionRefreshConfig:
    min_growth_turns: int = DEFAULT_MIN_GROWTH_TURNS
    max_threads_per_run: int = DEFAULT_MAX_THREADS_PER_RUN


async def load_effective_tenant_config(
    db: Any, tenant_row: dict[str, Any], request_key: str | None = None
):
    """Runtime tenant config: row merged with the published config version.

    Mirrors the conversation route's runtime read (Arch 12, P0-11): no
    published version -> the row is authoritative. ``request_key`` (end-user
    id or session id) selects between a canary rollout and its baseline
    (P5-2); background workers pass None and always see the latest published.
    """
    from backend.app.infrastructure.db import TenantConfigVersionRepository
    from backend.app.modules.tenant_config import tenant_config_from_data

    config = tenant_config_from_data(tenant_row)
    published = await TenantConfigVersionRepository(db).get_effective(
        str(config.id), request_key
    )
    if published:
        return tenant_config_from_data({**tenant_row, **published["config"]})
    return config


async def _count_since(db: Any, tenant_id: str, thread_id: str, after_seq: int) -> int:
    """Count turns newer than ``after_seq`` (bounded page walk)."""
    from backend.app.infrastructure.db import ThreadRepository

    threads = ThreadRepository(db)
    count = 0
    after = after_seq
    while True:
        page = await threads.list_messages(
            tenant_id, thread_id, after_seq=after, limit=200
        )
        messages = page["messages"]
        count += len(messages)
        if not page["has_more"]:
            return count
        after = messages[-1]["seq"]


async def refresh_thread_summary(
    db: Any,
    tenant_id: str,
    thread_id: str,
    *,
    generator: Any,
    config: CompactionRefreshConfig | None = None,
    compaction_config: Any | None = None,
    breaker: Any | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Idempotent per-thread refresh of the rolling summary.

    Returns ``{"refreshed": bool, "reason": str, ...checkpoint}``. The
    thread is skipped when it has not grown ``min_growth_turns`` past the
    last boundary (or has no summary yet); otherwise the head is
    re-summarized through the atomic swap (never a partial state).
    """
    from backend.app.application.compaction import (
        CompactionConfig,
        CompactionService,
    )
    from backend.app.infrastructure.db import ThreadRepository

    config = config or CompactionRefreshConfig()
    threads = ThreadRepository(db)
    thread = await threads.get_thread(tenant_id, thread_id)
    if thread is None:
        return {"refreshed": False, "reason": "thread_not_found"}

    position = thread.get("summary_position") or 0
    new_turns = await _count_since(db, tenant_id, thread_id, position)
    if new_turns < config.min_growth_turns:
        return {"refreshed": False, "reason": "insufficient_growth", "new_turns": new_turns}

    service = CompactionService(
        threads=threads,
        generator=generator,
        config=compaction_config or CompactionConfig(),
        breaker=breaker,
    )
    result = await service.compact(
        tenant_id, thread_id, request_id=request_id
    )
    return {"refreshed": bool(result.get("compacted")), "reason": "compacted", **result}


async def find_stale_threads(
    db: Any,
    config: CompactionRefreshConfig | None = None,
) -> list[dict[str, Any]]:
    """Threads whose newest message outgrew the compaction boundary."""
    from backend.app.infrastructure.db import ThreadRepository

    config = config or CompactionRefreshConfig()
    return await ThreadRepository(db).list_threads_stale_for_compaction(
        since_seq_delta=config.min_growth_turns,
        limit=config.max_threads_per_run,
    )


GeneratorBuilder = Callable[[Any, str], Any]


def default_generator_builder(tenant_config: Any, api_key: str) -> Any:
    """Build the real summarizer generator for a tenant's default model."""
    from backend.app.adapters.llm import (
        LLMConfig,
        LLMProviderType,
        create_llm_adapter,
    )
    from backend.app.application.compaction import LLMSummaryGenerator

    adapter = create_llm_adapter(
        LLMProviderType(tenant_config.default_provider),
        api_key,
        LLMConfig(model=tenant_config.default_model),
    )
    return LLMSummaryGenerator(adapter)
