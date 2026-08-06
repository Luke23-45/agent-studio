"""
DLP (Data Loss Prevention) adapters.

Provides PII detection and redaction services.
"""

from .presidio import PIIService, get_pii_service

__all__ = [
    "PIIService",
    "get_pii_service",
]