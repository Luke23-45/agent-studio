"""
Compaction package (Arch 8.2, P2-3/P2-4/P2-5): triggers, atomic summary
swap, breaker + lossy-truncation fallback, chunk-and-merge summarizer,
background summary refresh (worker job).
"""

from backend.app.application.compaction.breaker import (
    CompactionBreaker,
    CompactionBreakerConfig,
    get_compaction_breaker,
)
from backend.app.application.compaction.refresh import (
    CompactionRefreshConfig,
    JOB_SUMMARY_REFRESH,
    default_generator_builder,
    find_stale_threads,
    load_effective_tenant_config,
    refresh_thread_summary,
)
from backend.app.application.compaction.service import (
    CompactionAborted,
    CompactionConfig,
    CompactionDecision,
    CompactionError,
    CompactionHook,
    CompactionService,
    DEGRADED_SUMMARY_CONTENT,
    LLMSummaryGenerator,
    SUMMARY_FIELDS,
    SummarySchema,
    SummaryValidationError,
)

__all__ = [
    "CompactionAborted",
    "CompactionBreaker",
    "CompactionBreakerConfig",
    "CompactionConfig",
    "CompactionDecision",
    "CompactionError",
    "CompactionHook",
    "CompactionRefreshConfig",
    "CompactionService",
    "DEGRADED_SUMMARY_CONTENT",
    "JOB_SUMMARY_REFRESH",
    "LLMSummaryGenerator",
    "SUMMARY_FIELDS",
    "SummarySchema",
    "SummaryValidationError",
    "default_generator_builder",
    "find_stale_threads",
    "get_compaction_breaker",
    "load_effective_tenant_config",
    "refresh_thread_summary",
]
