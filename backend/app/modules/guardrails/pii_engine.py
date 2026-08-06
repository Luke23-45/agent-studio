import structlog
from typing import Any, Dict, List, Optional, Tuple

from ...infrastructure.patterns import ManagedService, AsyncRetry, RetryConfig
from .types import GuardrailLayer
from .errors import GuardrailEngineError

logger = structlog.get_logger(__name__)


class PIIEnhancementEngine(ManagedService):
    layer = GuardrailLayer.PII_REDACTION

    DEFAULT_PII_ENTITIES = ["PERSON", "EMAIL_ADDRESS", "PHONE_NUMBER", "CREDIT_CARD", "US_SSN", "LOCATION", "DATE_TIME"]

    def __init__(
        self,
        enabled_entities: Optional[List[str]] = None,
        score_threshold: float = 0.5,
        anonymize_action: str = "redact",
        presidio_timeout: float = 5.0,
    ):
        super().__init__("pii_engine")
        self.enabled_entities = enabled_entities or self.DEFAULT_PII_ENTITIES
        self.score_threshold = score_threshold
        self.anonymize_action = anonymize_action
        self.presidio_timeout = presidio_timeout
        self._analyzer = None
        self._anonymizer = None
        self._retry = AsyncRetry(RetryConfig(max_attempts=2))

    async def _do_initialize(self) -> None:
        try:
            from presidio_analyzer import AnalyzerEngine
            from presidio_anonymizer import AnonymizerEngine
            self._analyzer = AnalyzerEngine()
            self._anonymizer = AnonymizerEngine()
            logger.info("presidio_initialized")
        except ImportError as e:
            # Degrade instead of raising: PII redaction fails CLOSED at
            # evaluation time (see redact_pii()).
            logger.warning(
                "presidio_unavailable",
                reason="import_error",
                error=str(e),
                message="PII redaction degraded: output redaction will fail closed.",
            )
            self._analyzer = None
            self._anonymizer = None
        except Exception as e:
            logger.warning("presidio_init_error", error=str(e))
            self._analyzer = None
            self._anonymizer = None

    async def _do_close(self) -> None:
        self._analyzer = None
        self._anonymizer = None

    async def detect_pii(self, text: str) -> List[Dict[str, Any]]:
        if not self._analyzer:
            raise GuardrailEngineError(
                self.layer.name,
                "Presidio is unavailable; PII detection cannot run (fail-closed).",
            )
        try:
            results = await self._retry.execute(
                self._analyzer.analyze,
                text=text,
                entities=self.enabled_entities,
                score_threshold=self.score_threshold,
            )
            return [
                {
                    "entity_type": r.entity_type,
                    "start": r.start,
                    "end": r.end,
                    "score": r.score,
                    "text": text[r.start : r.end],
                }
                for r in results
            ]
        except Exception as e:
            logger.error("pii_detection_error", error=str(e))
            return []

    async def redact_pii(self, text: str) -> Tuple[str, List[Dict[str, Any]]]:
        detections = await self.detect_pii(text)
        if not detections or not self._anonymizer:
            return text, detections
        try:
            from presidio_anonymizer.entities import RecognizerResult, OperatorConfig
            recognizer_results = [
                RecognizerResult(
                    entity_type=d["entity_type"],
                    start=d["start"],
                    end=d["end"],
                    score=d["score"],
                )
                for d in detections
            ]
            anonymized = self._anonymizer.anonymize(
                text=text,
                analyzer_results=recognizer_results,
                operators={"DEFAULT": OperatorConfig(self.anonymize_action, {})},
            )
            return anonymized.text, detections
        except Exception as e:
            logger.error("pii_redaction_error", error=str(e))
            return text, detections

    async def _do_health_check(self) -> Dict[str, Any]:
        return {
            "presidio_loaded": self._analyzer is not None and self._anonymizer is not None,
            "entities_configured": len(self.enabled_entities),
            "score_threshold": self.score_threshold,
        }
