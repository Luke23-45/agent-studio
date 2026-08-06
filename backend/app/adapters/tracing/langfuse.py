"""
LangFuse observability adapter.

Provides tracing, evaluation, and monitoring integration with LangFuse.
"""

import structlog
from typing import Any
from uuid import UUID

from backend.app.settings.env import settings

logger = structlog.get_logger(__name__)


class LangFuseAdapter:
    """Adapter for LangFuse observability platform."""

    def __init__(
        self,
        public_key: str | None = None,
        secret_key: str | None = None,
        host: str | None = None,
    ):
        self.public_key = public_key or settings.LANGFUSE_PUBLIC_KEY
        self.secret_key = secret_key or settings.LANGFUSE_SECRET_KEY
        self.host = host or settings.LANGFUSE_HOST
        self._client: Any | None = None
        self._enabled = bool(self.public_key and self.secret_key)

        if not self._enabled:
            logger.warning("langfuse_disabled", reason="missing_credentials")

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    def _get_client(self) -> Any | None:
        """Lazy load LangFuse client."""
        if not self._enabled:
            return None

        if self._client is None:
            try:
                from langfuse import Langfuse
                self._client = Langfuse(
                    public_key=self.public_key,
                    secret_key=self.secret_key,
                    host=self.host,
                )
            except ImportError:
                logger.error("langfuse_import_error", message="langfuse package not installed")
                return None

        return self._client

    def trace(
        self,
        name: str,
        user_id: str | None = None,
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        tags: list[str] | None = None,
    ) -> Any | None:
        """Create a new trace."""
        client = self._get_client()
        if client is None:
            return None

        return client.trace(
            name=name,
            user_id=user_id,
            session_id=session_id,
            metadata=metadata or {},
            tags=tags or [],
        )

    def get_trace(self, trace_id: str) -> Any | None:
        """Get an existing trace by ID."""
        client = self._get_client()
        if client is None:
            return None

        return client.get_trace(trace_id)

    def update(
        self,
        trace: Any,
        input: Any | None = None,
        output: Any | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Any | None:
        """Update a trace's input/output/metadata after completion."""
        if trace is None:
            return None
        return trace.update(
            input=input,
            output=output,
            metadata=metadata or {},
        )

    def create_span(
        self,
        trace: Any,
        name: str,
        input: Any | None = None,
        output: Any | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Any | None:
        """Create a span within a trace."""
        if trace is None:
            return None

        return trace.span(
            name=name,
            input=input,
            output=output,
            metadata=metadata or {},
        )

    def create_generation(
        self,
        trace: Any,
        name: str,
        model: str,
        model_parameters: dict[str, Any] | None = None,
        input: Any | None = None,
        output: Any | None = None,
        usage: dict[str, int] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Any | None:
        """Create a generation event for LLM calls."""
        if trace is None:
            return None

        return trace.generation(
            name=name,
            model=model,
            model_parameters=model_parameters or {},
            input=input,
            output=output,
            usage=usage,
            metadata=metadata or {},
        )

    def score(
        self,
        trace_id: str,
        name: str,
        value: float,
        comment: str | None = None,
        data_type: str | None = None,
    ) -> None:
        """Add a score to a trace."""
        client = self._get_client()
        if client is None:
            return

        client.score(
            trace_id=trace_id,
            name=name,
            value=value,
            comment=comment,
            data_type=data_type,
        )

    def flush(self) -> None:
        """Flush all queued events."""
        client = self._get_client()
        if client:
            client.flush()

    def shutdown(self) -> None:
        """Shutdown the client gracefully."""
        client = self._get_client()
        if client:
            client.shutdown()


# Singleton instance
_langfuse_adapter: LangFuseAdapter | None = None


def get_langfuse_adapter() -> LangFuseAdapter:
    """Get or create LangFuse adapter instance."""
    global _langfuse_adapter
    if _langfuse_adapter is None:
        _langfuse_adapter = LangFuseAdapter()
    return _langfuse_adapter


def create_observation_context(
    tenant_id: UUID | None = None,
    conversation_id: str | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    """Create context metadata for observations."""
    return {
        "tenant_id": str(tenant_id) if tenant_id else None,
        "conversation_id": conversation_id,
        "user_id": user_id,
    }
