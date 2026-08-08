"""
Feature flags for Neryva Agent Studio.

Control experimental features and gradual rollouts.

Wiring status (verify before trusting a flag):
- Wired: ENABLE_NEMO_GUARDRAILS, ENABLE_GUARDRAILS_AI, ENABLE_PRESIDIO,
  ENABLE_SPOTLIGHTING, ENABLE_HUMAN_HANDOFF
- Not yet wired to a code path (kept for future rollouts):
  ENABLE_LLAMA_GUARD_4, ENABLE_CLOUD_DLP, ENABLE_RAGAS_EVAL,
  ENABLE_LANGFUSE_TRACING, ENABLE_GARAK_SCANS, ENABLE_PYRIT_ATTACKS,
  ENABLE_ISOLATED_DEPLOYMENT

Do not assume a flag listed as "not wired" is active. A flag that is True
but has no implementation must never be presented as enforced.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class FeatureFlags:
    """Feature flag configuration."""

    # Guardrails
    ENABLE_NEMO_GUARDRAILS: bool = True
    ENABLE_GUARDRAILS_AI: bool = True
    ENABLE_LLAMA_GUARD_4: bool = False

    # PII Handling
    ENABLE_PRESIDIO: bool = True
    ENABLE_CLOUD_DLP: bool = False

    # RAG
    ENABLE_SPOTLIGHTING: bool = True
    ENABLE_RAGAS_EVAL: bool = True

    # Observability
    ENABLE_LANGFUSE_TRACING: bool = True

    # Red Teaming
    ENABLE_GARAK_SCANS: bool = True
    ENABLE_PYRIT_ATTACKS: bool = True

    # Escalation
    ENABLE_HUMAN_HANDOFF: bool = False

    # Multi-tenancy
    ENABLE_ISOLATED_DEPLOYMENT: bool = True

    # Phase 9 (pulled forward): MCP servers as tool sources behind the
    # P5-3 gate (6.3). Default off; wired via backend/app/application/tools/factory.py
    # and the /tools admin API when enabled.
    ENABLE_MCP_TOOLS: bool = False

    # Phase 9 (pulled forward): hybrid retrieval + cross-encoder rerank
    # (7.2/7.3). Single-stage vector retrieval remains the default; flip
    # these after recall evals (P6-6) show a gap.
    ENABLE_HYBRID_RETRIEVAL: bool = False
    ENABLE_CROSS_ENCODER: bool = False


# Global feature flags instance
feature_flags = FeatureFlags()
