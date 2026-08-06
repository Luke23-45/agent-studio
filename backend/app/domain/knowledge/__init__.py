"""
Knowledge domain models and business logic.

Handles RAG knowledge base, document retrieval, and semantic memory.
"""

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID, uuid4


@dataclass
class Document:
    """A document in the knowledge base."""

    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID | None = None
    title: str = ""
    content: str = ""
    source: str = ""
    metadata: dict[str, str] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    embedding_id: str | None = None


KnowledgeDocument = Document


@dataclass
class RetrievedChunk:
    """A retrieved chunk of knowledge for RAG."""

    document_id: UUID
    content: str
    score: float
    metadata: dict[str, str] = field(default_factory=dict)

    def to_context_string(self) -> str:
        """Convert to a context string for the prompt."""
        return f"[Source: {self.metadata.get('source', 'unknown')}]\n{self.content}"


@dataclass
class KnowledgeQuery:
    """Query parameters for knowledge retrieval."""

    query_text: str
    tenant_id: UUID | None = None
    max_results: int = 5
    min_score: float = 0.7
    allowed_sources: list[str] = field(default_factory=list)
    filters: dict[str, Any] | None = None

    @property
    def text(self) -> str:
        """Backwards-compatible alias for query_text."""
        return self.query_text

    def is_source_allowed(self, source: str) -> bool:
        """Check if a source is allowed for this query."""
        if not self.allowed_sources:
            return True
        return source in self.allowed_sources
