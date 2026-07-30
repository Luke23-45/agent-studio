"""
Safety domain models and business logic.

Handles injection detection, jailbreak prevention, and content safety.
"""

from dataclasses import dataclass, field
from enum import Enum


class SafetyViolationType(Enum):
    """Types of safety violations."""

    PROMPT_INJECTION = "prompt_injection"
    JAILBREAK_ATTEMPT = "jailbreak_attempt"
    SYSTEM_PROMPT_LEAK = "system_prompt_leak"
    OFF_TOPIC = "off_topic"
    HARMFUL_CONTENT = "harmful_content"


@dataclass
class SafetyCheckResult:
    """Result of a safety check."""

    is_safe: bool = True
    violation_type: SafetyViolationType | None = None
    confidence: float = 1.0
    details: str = ""
    recommended_action: str = "allow"

    def should_block(self) -> bool:
        """Determine if the request should be blocked."""
        return not self.is_safe and self.recommended_action == "block"

    def should_escalate(self) -> bool:
        """Determine if the request should be escalated."""
        return not self.is_safe and self.recommended_action == "escalate"


@dataclass
class SafetyConfig:
    """Configuration for safety checks."""

    enable_injection_detection: bool = True
    enable_jailbreak_detection: bool = True
    enable_topic_filtering: bool = True
    injection_threshold: float = 0.7
    jailbreak_threshold: float = 0.7
    off_topic_threshold: float = 0.5
