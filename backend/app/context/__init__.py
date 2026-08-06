"""
Context assembly package (Arch 8.1, P2-1).

``SessionContextLoader`` is the single component that constructs LLM
provider messages; the orchestrator consumes it and nothing else builds
prompts (P2-1 acceptance). Model facts (context windows, price cards)
are owned by ``gateway.catalog`` and consumed here (P2-2).
"""

from backend.app.context.assembler import (
    AssembledContext,
    CACHE_CONTROL_METADATA,
    ContextBudgetError,
    ContextBudgetExceeded,
    ContextTurn,
    MEMORY_BLOCK_HEADER,
    RedactionViolation,
    SUMMARY_BLOCK_HEADER,
    TOOL_RESULT_PLACEHOLDER,
    SessionContextLoader,
)
from backend.app.context.estimator import (
    DEFAULT_CHARS_PER_TOKEN,
    TokenEstimator,
)
from backend.app.context.metrics import (
    PromptCacheMetricsCollector,
    TenantCacheCounters,
    get_prompt_cache_metrics,
)

__all__ = [
    "AssembledContext",
    "CACHE_CONTROL_METADATA",
    "ContextBudgetError",
    "ContextBudgetExceeded",
    "ContextTurn",
    "DEFAULT_CHARS_PER_TOKEN",
    "MEMORY_BLOCK_HEADER",
    "PromptCacheMetricsCollector",
    "RedactionViolation",
    "SUMMARY_BLOCK_HEADER",
    "SessionContextLoader",
    "TenantCacheCounters",
    "TOOL_RESULT_PLACEHOLDER",
    "TokenEstimator",
    "get_prompt_cache_metrics",
]
