"""
PII domain models and business logic.

Handles personally identifiable information detection, redaction, and data handling.
"""

from dataclasses import dataclass, field
from enum import Enum


class PIICategory(Enum):
    """Categories of PII."""

    EMAIL = "email"
    PHONE = "phone"
    SSN = "ssn"
    CREDIT_CARD = "credit_card"
    IP_ADDRESS = "ip_address"
    NAME = "name"
    ADDRESS = "address"
    DATE_OF_BIRTH = "date_of_birth"
    MEDICAL_RECORD = "medical_record"
    FINANCIAL_DATA = "financial_data"


@dataclass
class PIIDetection:
    """Detected PII in text."""

    category: PIICategory
    value: str
    start_index: int
    end_index: int
    confidence: float
    redacted_value: str | None = None


@dataclass
class PIIRedactionResult:
    """Result of PII redaction."""

    original_text: str
    redacted_text: str
    detections: list[PIIDetection] = field(default_factory=list)
    redaction_strategy: str = "mask"

    def has_pii(self) -> bool:
        """Check if any PII was detected."""
        return len(self.detections) > 0


@dataclass
class PIIConfig:
    """Configuration for PII handling."""

    enable_detection: bool = True
    enable_redaction: bool = True
    enabled_categories: list[PIICategory] = field(
        default_factory=lambda: [
            PIICategory.EMAIL,
            PIICategory.PHONE,
            PIICategory.SSN,
            PIICategory.CREDIT_CARD,
        ]
    )
    redaction_mask: str = "[REDACTED]"
    min_confidence: float = 0.7
