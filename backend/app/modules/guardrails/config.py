from dataclasses import dataclass, field
from typing import Optional

from ...domain.tenant import TenantConfig
from ...settings.feature_flags import feature_flags
from .errors import GuardrailConfigurationError


@dataclass
class GuardrailsModuleConfig:
    enable_regex_fastpath: bool = True
    enable_classifier: bool = True
    enable_nemo_rails: bool = False
    enable_jailbreak_scan: bool = True
    enable_guardrails_ai: bool = True
    enable_pii_redaction: bool = True
    enable_spotlighting: bool = True

    classifier_threshold: float = 0.5
    jailbreak_threshold: float = 0.7
    pii_threshold: float = 0.5

    layer_timeout_seconds: float = 5.0
    parallel_layer_execution: bool = True
    fail_open_on_error: bool = False

    circuit_breaker_failure_threshold: int = 5
    circuit_breaker_recovery_timeout: float = 30.0

    rate_limit_max_requests: int = 200
    rate_limit_window_seconds: float = 60.0

    nemo_config_path: Optional[str] = None

    def validate(self) -> None:
        if self.classifier_threshold < 0.0 or self.classifier_threshold > 1.0:
            raise GuardrailConfigurationError("classifier_threshold", "must be in [0.0, 1.0]")
        if self.jailbreak_threshold < 0.0 or self.jailbreak_threshold > 1.0:
            raise GuardrailConfigurationError("jailbreak_threshold", "must be in [0.0, 1.0]")
        if self.pii_threshold < 0.0 or self.pii_threshold > 1.0:
            raise GuardrailConfigurationError("pii_threshold", "must be in [0.0, 1.0]")
        if self.layer_timeout_seconds <= 0.0:
            raise GuardrailConfigurationError("layer_timeout_seconds", "must be > 0.0")
        if self.circuit_breaker_failure_threshold < 1:
            raise GuardrailConfigurationError("circuit_breaker_failure_threshold", "must be >= 1")
        if self.rate_limit_max_requests < 1:
            raise GuardrailConfigurationError("rate_limit_max_requests", "must be >= 1")
        # Fail-closed: enabling NeMo without a rails config would silently
        # claim enforcement that does not exist. Refuse to boot instead.
        if self.enable_nemo_rails and not self.nemo_config_path:
            raise GuardrailConfigurationError(
                "nemo_config_path",
                "enable_nemo_rails=True requires nemo_config_path",
            )


def build_config_from_tenant(tenant_config: TenantConfig, nemo_config_path: Optional[str] = None) -> GuardrailsModuleConfig:
    return GuardrailsModuleConfig(
        enable_regex_fastpath=tenant_config.is_guardrail_enabled("regex_fastpath"),
        enable_classifier=tenant_config.is_guardrail_enabled("classifier"),
        enable_nemo_rails=(
            tenant_config.is_guardrail_enabled("nemo_rails")
            and feature_flags.ENABLE_NEMO_GUARDRAILS
        ),
        enable_jailbreak_scan=tenant_config.is_guardrail_enabled("jailbreak_detection"),
        enable_guardrails_ai=(
            tenant_config.is_guardrail_enabled("output_validation")
            and feature_flags.ENABLE_GUARDRAILS_AI
        ),
        enable_pii_redaction=(
            tenant_config.is_guardrail_enabled("pii_detection")
            and feature_flags.ENABLE_PRESIDIO
        ),
        enable_spotlighting=(
            tenant_config.is_guardrail_enabled("spotlighting")
            and feature_flags.ENABLE_SPOTLIGHTING
        ),
        classifier_threshold=tenant_config.get_guardrail_threshold("classifier"),
        jailbreak_threshold=tenant_config.get_guardrail_threshold("jailbreak"),
        pii_threshold=tenant_config.get_guardrail_threshold("pii"),
        nemo_config_path=nemo_config_path,
    )
