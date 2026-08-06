"""
Compaction package (Arch 8.2, P2-3/P2-4/P2-5/P2-10): triggers, atomic
summary swap, breaker + lossy-truncation fallback, chunk-and-merge
summarizer, background summary refresh (worker job), and quality evals
(round-trip fact retention, tuning harness, context-rot monitor).
"""

from backend.app.application.compaction.breaker import (
    CompactionBreaker,
    CompactionBreakerConfig,
    get_compaction_breaker,
)
from backend.app.application.compaction.eval import (
    DEFAULT_QUALITY_THRESHOLD,
    CheckpointEvalResult,
    CompactionQualityEvaluator,
    CompactionQualityMonitor,
    EvalCase,
    EvalCaseResult,
    FactProbe,
    FactVerdict,
    LexicalFactScorer,
    LLMJudgeScorer,
    ProbeExtractionError,
    TuningResult,
    aggregate_results,
    generate_probes_from_turns,
    load_dataset,
    run_tuning_harness,
)
from backend.app.application.compaction.refresh import (
    JOB_SUMMARY_REFRESH,
    CompactionRefreshConfig,
    default_generator_builder,
    find_stale_threads,
    load_effective_tenant_config,
    refresh_thread_summary,
)
from backend.app.application.compaction.service import (
    DEGRADED_SUMMARY_CONTENT,
    SUMMARY_FIELDS,
    CompactionAborted,
    CompactionConfig,
    CompactionDecision,
    CompactionError,
    CompactionHook,
    CompactionService,
    LLMSummaryGenerator,
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
    "CompactionQualityEvaluator",
    "CompactionQualityMonitor",
    "CompactionRefreshConfig",
    "CompactionService",
    "DEFAULT_QUALITY_THRESHOLD",
    "DEGRADED_SUMMARY_CONTENT",
    "EvalCase",
    "EvalCaseResult",
    "FactProbe",
    "FactVerdict",
    "JOB_SUMMARY_REFRESH",
    "LLMJudgeScorer",
    "LLMSummaryGenerator",
    "LexicalFactScorer",
    "ProbeExtractionError",
    "SUMMARY_FIELDS",
    "SummarySchema",
    "SummaryValidationError",
    "TuningResult",
    "aggregate_results",
    "default_generator_builder",
    "find_stale_threads",
    "generate_probes_from_turns",
    "get_compaction_breaker",
    "load_dataset",
    "load_effective_tenant_config",
    "refresh_thread_summary",
    "run_tuning_harness",
    "CheckpointEvalResult",
]
