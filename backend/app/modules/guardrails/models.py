from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ...domain.safety import SafetyCheckResult, SafetyViolation
from .types import GuardrailLayer, GuardrailDecision


@dataclass(frozen=True)
class GuardrailConfig:
    layer: GuardrailLayer
    enabled: bool = True
    threshold: float = 0.5
    action: GuardrailDecision = GuardrailDecision.BLOCK
    timeout_seconds: float = 5.0
    message: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.threshold < 0.0 or self.threshold > 1.0:
            raise ValueError(f"threshold must be in [0.0, 1.0], got {self.threshold}")
        if self.timeout_seconds <= 0.0:
            raise ValueError(f"timeout_seconds must be > 0.0, got {self.timeout_seconds}")


@dataclass
class GuardrailEvaluationResult:
    allowed: bool
    decision: GuardrailDecision
    layer_results: Dict[GuardrailLayer, SafetyCheckResult] = field(default_factory=dict)
    violations: List[SafetyViolation] = field(default_factory=list)
    redacted_text: Optional[str] = None
    confidence_score: float = 0.0
    processing_time_ms: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def add_layer_result(self, layer: GuardrailLayer, result: SafetyCheckResult) -> None:
        self.layer_results[layer] = result
        if not result.passed:
            self.violations.extend(result.violations)
            for v in result.violations:
                if v.severity.name in ("HIGH", "CRITICAL"):
                    self.decision = GuardrailDecision.BLOCK
                    self.allowed = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "allowed": self.allowed,
            "decision": self.decision.name,
            "violations": [v.to_dict() for v in self.violations],
            "redacted_text": self.redacted_text,
            "confidence_score": self.confidence_score,
            "processing_time_ms": self.processing_time_ms,
            "layers_evaluated": [l.name for l in self.layer_results.keys()],
            "metadata": self.metadata,
        }
