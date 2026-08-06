from enum import Enum, auto


class GuardrailLayer(Enum):
    REGEX_FASTPATH = auto()
    CLASSIFIER = auto()
    NEMO_RAILS = auto()
    JAILBREAK_SCAN = auto()
    GUARDRAILS_AI = auto()
    PII_REDACTION = auto()
    SPOTLIGHTING = auto()


class GuardrailDecision(Enum):
    ALLOW = auto()
    BLOCK = auto()
    REDIRECT = auto()
    REDACT = auto()
    ESCALATE = auto()
    REASK = auto()
