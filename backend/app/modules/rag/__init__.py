"""
RAG module.

Provides retrieval augmented generation capabilities.
"""

from ...adapters.vectorstore.provider import VectorSearchResult

from .service import (
    EmbeddingService,
    RAGService,
    create_rag_service,
)

__all__ = [
    "EmbeddingService",
    "RAGService",
    "VectorSearchResult",
    "create_rag_service",
]