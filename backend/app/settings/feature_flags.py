"""
Feature flags for Neryva Agent Studio.

Control experimental features and gradual rollouts.
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
