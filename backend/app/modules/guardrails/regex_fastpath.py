import asyncio
import re
import structlog
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ...domain.safety import SafetyCheckResult, SafetyViolation, ViolationSeverity, SafetyCategory
from ...domain.tenant import TenantConfig
from ...infrastructure.patterns import ManagedService
from .types import GuardrailLayer, GuardrailDecision
from .errors import GuardrailConfigurationError, GuardrailTimeoutError

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class RegexPattern:
    name: str
    pattern: str
    category: SafetyCategory
    severity: ViolationSeverity
    action: GuardrailDecision
    description: str = ""
    _compiled: Optional[re.Pattern] = None

    def __post_init__(self) -> None:
        if not self.name:
            raise GuardrailConfigurationError("name", "RegexPattern name cannot be empty")
        if not self.pattern:
            raise GuardrailConfigurationError("pattern", "RegexPattern pattern cannot be empty")

    @property
    def compiled(self) -> re.Pattern:
        if self._compiled is None:
            object.__setattr__(self, "_compiled", re.compile(self.pattern))
        return self._compiled


class RegexFastpathEngine(ManagedService):
    layer = GuardrailLayer.REGEX_FASTPATH

    DEFAULT_PATTERNS: List[RegexPattern] = [
        RegexPattern(
            name="obvious_injection",
            pattern=r"(?i)(ignore previous|disregard above|system prompt|you are now)",
            category=SafetyCategory.PROMPT_INJECTION,
            severity=ViolationSeverity.HIGH,
            action=GuardrailDecision.BLOCK,
            description="Obvious injection attempt keywords",
        ),
        RegexPattern(
            name="profanity_basic",
            pattern=r"(?i)(fuck|shit|bitch|asshole)",
            category=SafetyCategory.PROFANITY,
            severity=ViolationSeverity.MEDIUM,
            action=GuardrailDecision.REDIRECT,
            description="Basic profanity detection",
        ),
        RegexPattern(
            name="pii_leak_keywords",
            pattern=r"(?i)(credit.card|social.security|ssn|passport|driver.?s license)",
            category=SafetyCategory.HARMFUL_CONTENT,
            severity=ViolationSeverity.HIGH,
            action=GuardrailDecision.REDACT,
            description="Potential PII leak keywords",
        ),
        RegexPattern(
            name="url_injection",
            pattern=r"(?i)(https?://|www\.)\S+",
            category=SafetyCategory.PROMPT_INJECTION,
            severity=ViolationSeverity.MEDIUM,
            action=GuardrailDecision.REASK,
            description="URL in user input - verify before processing",
        ),
    ]

    def __init__(self, custom_patterns: Optional[List[RegexPattern]] = None, timeout: float = 1.0):
        super().__init__("regex_fastpath")
        self._patterns = list(self.DEFAULT_PATTERNS)
        if custom_patterns:
            for p in custom_patterns:
                if not any(dp.name == p.name for dp in self._patterns):
                    self._patterns.append(p)
        self._timeout = timeout

    async def _do_initialize(self) -> None:
        for p in self._patterns:
            _ = p.compiled
        logger.info("regex_patterns_compiled", count=len(self._patterns))

    async def _do_close(self) -> None:
        self._patterns.clear()

    async def evaluate(self, text: str, tenant_config: TenantConfig, context: Optional[Dict[str, Any]] = None) -> SafetyCheckResult:
        start_time = asyncio.get_event_loop().time()
        if not text:
            return SafetyCheckResult(passed=True, violations=[], confidence=1.0, processing_time_ms=0.0)

        if not tenant_config.is_guardrail_enabled("regex_fastpath"):
            return SafetyCheckResult(
                passed=True, violations=[], confidence=1.0,
                processing_time_ms=(asyncio.get_event_loop().time() - start_time) * 1000,
                metadata={"layer": self.layer.name, "reason": "layer_disabled"},
            )

        violations: List[SafetyViolation] = []
        matched_patterns: List[str] = []

        for pattern_def in self._patterns:
            try:
                match = await asyncio.wait_for(
                    asyncio.get_event_loop().run_in_executor(None, pattern_def.compiled.search, text),
                    timeout=self._timeout,
                )
                if match:
                    violation = SafetyViolation(
                        category=pattern_def.category,
                        severity=pattern_def.severity,
                        description=pattern_def.description,
                        matched_text=match.group(0),
                        position=(match.start(), match.end()),
                        metadata={"pattern_name": pattern_def.name},
                    )
                    violations.append(violation)
                    matched_patterns.append(pattern_def.name)
                    if pattern_def.severity == ViolationSeverity.CRITICAL:
                        break
            except asyncio.TimeoutError:
                raise GuardrailTimeoutError(self.layer.name, self._timeout) from None

        elapsed_ms = (asyncio.get_event_loop().time() - start_time) * 1000
        return SafetyCheckResult(
            passed=len(violations) == 0,
            violations=violations,
            confidence=0.0 if violations else 1.0,
            processing_time_ms=elapsed_ms,
            metadata={
                "layer": self.layer.name,
                "patterns_checked": len(self._patterns),
                "matched_patterns": matched_patterns,
            },
        )

    async def _do_health_check(self) -> Dict[str, Any]:
        return {
            "patterns_loaded": len(self._patterns),
            "timeout": self._timeout,
            "sample_patterns": [p.name for p in self._patterns[:3]],
        }

    def add_pattern(self, pattern: RegexPattern) -> None:
        if not any(p.name == pattern.name for p in self._patterns):
            self._patterns.append(pattern)
            _ = pattern.compiled

    def remove_pattern(self, name: str) -> bool:
        for i, p in enumerate(self._patterns):
            if p.name == name and p not in self.DEFAULT_PATTERNS:
                self._patterns.pop(i)
                return True
        return False
