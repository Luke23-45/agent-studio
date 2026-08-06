import asyncio
import re
import structlog
from typing import Any, Dict, List, Optional

from ...domain.safety import SafetyCheckResult, SafetyViolation, ViolationSeverity, SafetyCategory
from ...infrastructure.patterns import ManagedService
from .types import GuardrailLayer
from .errors import GuardrailTimeoutError

logger = structlog.get_logger(__name__)


class JailbreakDetector(ManagedService):
    layer = GuardrailLayer.JAILBREAK_SCAN

    JAILBREAK_PATTERNS = [
        (r"(?i)ignore\s+(previous|all)\s+(instructions|rules)", "ignore_instructions"),
        (r"(?i)disregard\s+(the\s+)?(above|previous)", "disregard_previous"),
        (r"(?i)you\s+are\s+now\s+(in\s+)?(mode|state|character)", "role_change"),
        (r"(?i)(system|developer)\s+prompt", "system_prompt_leak"),
        (r"(?i)bypass\s+(safety|security|filters)", "bypass_attempt"),
        (r"(?i)(dan|do\s+anything\s+now)", "dan_mode"),
        (r"(?i)roleplay\s+as\s+(unrestricted|uncensored)", "roleplay_unrestricted"),
        (r"(?i)print\s+(the\s+)?(system|developer)\s+(message|instruction)", "print_prompt"),
        (r"(?i)output\s+(your|the)\s+(prompt|instructions|system\s+message)", "output_prompt"),
        (r"(?i)reveal\s+(your|the)\s+(prompt|instructions|system\s+message|rules)", "reveal_prompt"),
        (r"(?i)simulate\s+(the\s+)?(ai|assistant|bot)\s+(without|bypassing)", "simulate_bypass"),
        (r"(?i)you\s+(don.?t|do\s+not)\s+have\s+to\s+(follow|obey)", "refuse_obey"),
        (r"(?i)new\s+rule[s]?\s*:?\s*you\s+(will\s+)?(now|must)", "new_rule_injection"),
        (r"(?i)this\s+is\s+(a\s+)?(test|simulation|roleplay|scenario)", "scenario_framing"),
    ]

    def __init__(self, scan_timeout: float = 2.0, heuristic_enabled: bool = True, heuristic_threshold: float = 0.7):
        super().__init__("jailbreak_detector")
        self.scan_timeout = scan_timeout
        self.heuristic_enabled = heuristic_enabled
        self.heuristic_threshold = heuristic_threshold
        self._compiled: List[re.Pattern] = []

    async def _do_initialize(self) -> None:
        self._compiled = [re.compile(p[0]) for p in self.JAILBREAK_PATTERNS]
        logger.info("jailbreak_patterns_compiled", count=len(self._compiled))

    async def _do_close(self) -> None:
        self._compiled.clear()

    async def evaluate(self, text: str, tenant_config=None, context=None) -> SafetyCheckResult:
        start_time = asyncio.get_event_loop().time()
        if not text:
            return SafetyCheckResult(passed=True, violations=[], confidence=1.0, processing_time_ms=0.0)

        violations: List[SafetyViolation] = []
        matched_patterns: List[str] = []

        try:
            for i, (_, name) in enumerate(self.JAILBREAK_PATTERNS):
                match = await asyncio.wait_for(
                    asyncio.get_event_loop().run_in_executor(None, self._compiled[i].search, text),
                    timeout=self.scan_timeout,
                )
                if match:
                    violations.append(SafetyViolation(
                        category=SafetyCategory.PROMPT_INJECTION,
                        severity=ViolationSeverity.HIGH,
                        description=f"Jailbreak pattern detected: {name}",
                        matched_text=match.group(0),
                        position=(match.start(), match.end()),
                        metadata={"detection_method": "regex_pattern", "pattern_name": name},
                    ))
                    matched_patterns.append(name)
        except asyncio.TimeoutError:
            raise GuardrailTimeoutError(self.layer.name, self.scan_timeout) from None

        if self.heuristic_enabled:
            heuristic_score = await asyncio.get_event_loop().run_in_executor(None, self._compute_heuristic_score, text)
            if heuristic_score > self.heuristic_threshold:
                violations.append(SafetyViolation(
                    category=SafetyCategory.PROMPT_INJECTION,
                    severity=ViolationSeverity.MEDIUM,
                    description=f"Heuristic jailbreak score exceeded threshold",
                    metadata={"detection_method": "heuristic", "score": heuristic_score},
                ))
        else:
            heuristic_score = 0.0

        elapsed_ms = (asyncio.get_event_loop().time() - start_time) * 1000
        return SafetyCheckResult(
            passed=len(violations) == 0,
            violations=violations,
            confidence=0.0 if violations else 0.95,
            processing_time_ms=elapsed_ms,
            metadata={
                "layer": self.layer.name,
                "patterns_checked": len(self._compiled),
                "matched_patterns": matched_patterns,
                "heuristic_score": heuristic_score,
            },
        )

    def _compute_heuristic_score(self, text: str) -> float:
        score = 0.0
        length = len(text)
        if length > 500:
            score += min(0.2, (length - 500) / 5000 * 0.2)
        instruction_words = ["ignore", "disregard", "override", "bypass", "pretend", "act as", "you are", "from now on"]
        score += sum(0.05 for w in instruction_words if w in text.lower())
        urgency_words = ["immediately", "now", "urgent", "as fast as possible"]
        score += sum(0.03 for w in urgency_words if w in text.lower())
        formatting_chars = sum(1 for c in text if c in '"\'\n\t')
        if formatting_chars > 20:
            score += 0.1
        return min(1.0, score)

    async def _do_health_check(self) -> Dict[str, Any]:
        return {
            "patterns_loaded": len(self._compiled),
            "heuristic_enabled": self.heuristic_enabled,
            "scan_timeout": self.scan_timeout,
        }
