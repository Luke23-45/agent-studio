"""
Rolling-window output moderation (Arch 9.1, Phase 4).

The stream is *not* emitted verbatim as tokens arrive. ``StreamingModerationWindow``
holds deltas until at least ``window_chars`` worth accumulate, validates that
window asynchronously (PII redaction + policy), and only then releases text to
the client — so the customer never sees content that has not passed the window
(arch 9.1 step 7).

``push`` returns ``None`` while a window is held, then a ``WindowRelease`` once a
window is validated. On a policy/guardrail violation the window marks the stream
``truncated``: the caller stops the upstream generation and emits a ``redaction``
event instead of the tail. PII redaction (``allowed`` with a ``redacted_text``)
releases the redacted text — still streamed, never the raw payload.

Validator protocol: ``async (text: str) -> result`` where ``result.allowed``
(bool) and ``result.redacted_text`` (``str | None``) and ``result.violations``
(iterable). When ``validator`` is None the window is disabled and chunks pass
through untouched (per-tenant/feature switch, matching the non-streaming path).
A validator exception fails open (text released raw, logged) so a flaky
guardrail layer never kills a healthy stream silently.
"""

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

import structlog

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class WindowRelease:
    """One validated portion of the stream, safe to emit to the client."""

    text: str
    # ``released_raw`` True when the window passed without any change.
    released_raw: bool = True
    # ``redacted`` True when PII/secret redaction was applied (stream continues).
    redacted: bool = False
    # ``truncated`` True when a policy/blocked violation ended the stream here.
    truncated: bool = False
    violations: list[Any] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        """True when the caller must stop generating (hard stop)."""
        return self.truncated


ValidatorCallable = Callable[[str], Awaitable[Any]]


class StreamingModerationWindow:
    """Staged release of a streaming response through an async validator."""

    def __init__(
        self,
        validator: Optional[ValidatorCallable] = None,
        *,
        window_size: int = 400,
        fallback_text: str = "[content withheld]",
    ):
        if window_size <= 0:
            raise ValueError(f"window_size must be > 0, got {window_size}")
        self._validator = validator
        self._window_size = window_size
        self._fallback_text = fallback_text
        self._pending = ""
        self._truncated = False

    @property
    def enabled(self) -> bool:
        return self._validator is not None

    @property
    def truncated(self) -> bool:
        return self._truncated

    async def push(self, chunk: str) -> Optional[WindowRelease]:
        """Feed one delta. Returns ``None`` (held) or a ``WindowRelease``."""
        if self._truncated:
            return None
        if not self.enabled:
            return WindowRelease(text=chunk, released_raw=True)
        self._pending += chunk
        if len(self._pending) < self._window_size:
            return None
        return await self._validate_window()

    async def finish(self) -> Optional[WindowRelease]:
        """Release whatever text is still held at stream end. Returns None
        when nothing is pending (or the window already truncated)."""
        if self._truncated or not self._pending:
            return None
        return await self._validate_window()

    async def _validate_window(self) -> WindowRelease:
        pending, self._pending = self._pending, ""
        try:
            result = await self._validator(pending)
            if result.allowed:
                redacted = getattr(result, "redacted_text", None)
                if redacted is not None:
                    return WindowRelease(
                        text=redacted, released_raw=False, redacted=True
                    )
                return WindowRelease(text=pending, released_raw=True)
            self._truncated = True
            return WindowRelease(
                text=self._fallback_text,
                released_raw=False,
                truncated=True,
                violations=list(getattr(result, "violations", None) or []),
            )
        except Exception as e:
            # Fail open with a visible signal: never kill a stream on a
            # broken guardrail layer (P0-7 semantics: non-authoritative).
            logger.error("stream_moderation_validator_failed", error=str(e))
            return WindowRelease(text=pending, released_raw=True)