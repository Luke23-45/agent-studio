"""
RAG module.

Provides retrieval augmented generation capabilities.
"""

from .service import (
    EmbeddingService,
    RAGService,
    create_rag_service,
)

__all__ = [
    "EmbeddingService",
    "RAGService",
    "create_rag_service",
]