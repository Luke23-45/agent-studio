"""
Token estimation (Arch 8.1, P2-1/P2-2).

The estimator drives the context assembler's budget math using the
4-characters-per-token heuristic (the same heuristic OpenCode v2 uses for
its preflight compaction estimate). Per-model chars-per-token overrides
live here; per-model context windows and price cards live in the
gateway/catalog module — the single source of model facts (P2-2).
"""

from __future__ import annotations

import math
from typing import Any, Sequence

DEFAULT_CHARS_PER_TOKEN: float = 4.0
"""OpenCode-style estimate: ``ceil(len(text) / 4)`` tokens."""


class TokenEstimator:
    """Character-based token estimator with per-model overrides.

    Default is 4 characters per token (OpenCode v2 heuristic). Overrides
    can be registered per provider or per provider:model.
    """

    def __init__(
        self,
        chars_per_token: dict[str, float] | None = None,
    ):
        self._overrides = {k.strip().lower(): v for k, v in (chars_per_token or {}).items()}

    @staticmethod
    def _key(provider: str, model: str) -> str:
        return f"{provider.strip().lower()}:{model.strip().lower()}"

    def chars_per_token(self, provider: str | None = None, model: str | None = None) -> float:
        if provider and model:
            value = self._overrides.get(self._key(provider, model))
            if value is not None:
                return value
        if provider:
            value = self._overrides.get(provider.strip().lower())
            if value is not None:
                return value
        return self._overrides.get("default", DEFAULT_CHARS_PER_TOKEN)

    def estimate(self, text: str, provider: str | None = None, model: str | None = None) -> int:
        """Estimate tokens for a single string."""
        if not text:
            return 0
        per = self.chars_per_token(provider, model)
        if per <= 0:
            return 0
        return math.ceil(len(text) / per)

    def estimate_messages(
        self,
        messages: Sequence[dict[str, Any]],
        provider: str | None = None,
        model: str | None = None,
    ) -> int:
        """Estimate tokens for a message list, counting parts, not raw text.

        Each message is counted individually (never concatenated, which
        would undercount token boundaries); non-string payloads (tool
        payloads, structured parts) are counted by their serialized size.
        """
        import json

        total = 0
        for message in messages:
            content = message.get("content")
            if isinstance(content, str):
                total += self.estimate(content, provider, model)
            elif content is not None:
                total += self.estimate(
                    json.dumps(content, ensure_ascii=False, sort_keys=True),
                    provider,
                    model,
                )
        return total

    def register_override(self, key: str, chars_per_token: float) -> None:
        """Register a chars-per-token override for a provider or provider:model."""
        self._overrides[key.strip().lower()] = chars_per_token
