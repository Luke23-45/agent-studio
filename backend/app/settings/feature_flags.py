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


# Global feature flags instance
feature_flags = FeatureFlags()
