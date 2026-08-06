from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Tuple


class SafetyCategory(Enum):
    PROMPT_INJECTION = auto()
    PROFANITY = auto()
    OFF_TOPIC = auto()
    HARMFUL_CONTENT = auto()
    JAILBREAK_ATTEMPT = auto()
    SYSTEM_PROMPT_LEAK = auto()
    SCHEMA_VALIDATION = auto()


class ViolationSeverity(Enum):
    LOW = auto()
    MEDIUM = auto()
    HIGH = auto()
    CRITICAL = auto()


@dataclass
class SafetyViolation:
    category: SafetyCategory
    severity: ViolationSeverity
    description: str = ""
    matched_text: Optional[str] = None
    position: Optional[Tuple[int, int]] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "category": self.category.name,
            "severity": self.severity.name,
            "description": self.description,
            "matched_text": self.matched_text,
            "position": list(self.position) if self.position else None,
            "metadata": self.metadata,
        }


@dataclass
class SafetyCheckResult:
    passed: bool
    violations: List[SafetyViolation] = field(default_factory=list)
    confidence: float = 1.0
    processing_time_ms: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def should_block(self) -> bool:
        return not self.passed and any(
            v.severity in (ViolationSeverity.HIGH, ViolationSeverity.CRITICAL)
            for v in self.violations
        )

    def should_escalate(self) -> bool:
        return not self.passed and any(
            v.severity == ViolationSeverity.MEDIUM for v in self.violations
        )
