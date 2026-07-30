"""
PII detection and redaction service using Microsoft Presidio.

Handles detection, redaction, and analysis of personally identifiable information.
"""

import structlog
from presidio_analyzer import AnalyzerEngine, EntityRecognizer, RecognizerResult
from presidio_analyzer.recognizer_registry import RecognizerRegistry
from presidio_anonymizer import AnonymizerEngine
from presidio_anonymizer.entities import OperatorConfig

from backend.app.domain.pii import PIICategory, PIIDetection, PIIRedactionResult, PIIConfig

logger = structlog.get_logger(__name__)


class PIIService:
    """Service for PII detection and redaction."""

    def __init__(self, config: PIIConfig | None = None):
        self.config = config or PIIConfig()
        self.analyzer = AnalyzerEngine()
        self.anonymizer = AnonymizerEngine()
        self._setup_recognizers()

    def _setup_recognizers(self) -> None:
        """Configure custom recognizers if needed."""
        # Can add custom entity recognizers here
        pass

    def _map_presidio_entity_to_category(self, entity_name: str) -> PIICategory | None:
        """Map Presidio entity names to our PIICategory enum."""
        mapping = {
            "EMAIL_ADDRESS": PIICategory.EMAIL,
            "PHONE_NUMBER": PIICategory.PHONE,
            "US_SSN": PIICategory.SSN,
            "CREDIT_CARD": PIICategory.CREDIT_CARD,
            "IP_ADDRESS": PIICategory.IP_ADDRESS,
            "PERSON": PIICategory.NAME,
            "LOCATION": PIICategory.ADDRESS,
            "DATE_TIME": PIICategory.DATE_OF_BIRTH,
            "MEDICAL_LICENSE_NUMBER": PIICategory.MEDICAL_RECORD,
            "US_BANK_NUMBER": PIICategory.FINANCIAL_DATA,
        }
        return mapping.get(entity_name.upper())

    def detect(self, text: str) -> list[PIIDetection]:
        """Detect PII in text."""
        if not self.config.enable_detection:
            return []

        results = self.analyzer.analyze(
            text=text,
            language="en",
            entities=[e.value for e in self.config.enabled_categories],
            score_threshold=self.config.min_confidence,
        )

        detections = []
        for result in results:
            category = self._map_presidio_entity_to_category(result.entity_type)
            if category is None:
                continue

            detected_value = text[result.start : result.end]
            detection = PIIDetection(
                category=category,
                value=detected_value,
                start_index=result.start,
                end_index=result.end,
                confidence=result.score,
            )
            detections.append(detection)

        logger.info("pii_detected", count=len(detections), text_length=len(text))
        return detections

    def redact(self, text: str, detections: list[PIIDetection] | None = None) -> PIIRedactionResult:
        """Redact PII from text."""
        if not self.config.enable_redaction:
            return PIIRedactionResult(
                original_text=text,
                redacted_text=text,
                detections=detections or [],
                redaction_strategy="none",
            )

        # If detections not provided, detect first
        if detections is None:
            detections = self.detect(text)

        if not detections:
            return PIIRedactionResult(
                original_text=text,
                redacted_text=text,
                detections=[],
                redaction_strategy="none",
            )

        # Convert to Presidio recognizer results
        recognizer_results = []
        for detection in detections:
            recognizer_result = RecognizerResult(
                entity_type=detection.category.value,
                start=detection.start_index,
                end=detection.end_index,
                score=detection.confidence,
            )
            recognizer_results.append(recognizer_result)

        # Anonymize
        anonymized_result = self.anonymizer.anonymize(
            text=text,
            analyzer_results=recognizer_results,
            operators={
                "DEFAULT": OperatorConfig("replace", {"new_value": self.config.redaction_mask})
            },
        )

        return PIIRedactionResult(
            original_text=text,
            redacted_text=anonymized_result.text,
            detections=detections,
            redaction_strategy="mask",
        )

    def process_message(self, text: str) -> PIIRedactionResult:
        """Full pipeline: detect and redact PII in a message."""
        detections = self.detect(text)
        return self.redact(text, detections)

    def get_supported_categories(self) -> list[PIICategory]:
        """Get list of supported PII categories."""
        return list(PIICategory)


# Singleton instance for easy access
_pii_service: PIIService | None = None


def get_pii_service(config: PIIConfig | None = None) -> PIIService:
    """Get or create PII service instance."""
    global _pii_service
    if _pii_service is None:
        _pii_service = PIIService(config)
    return _pii_service
