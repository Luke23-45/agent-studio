"""
Vector store adapters.

Provides unified interface for different vector databases (pgvector, Qdrant, etc.).
"""

from .provider import (
    BaseVectorStore,
    PGVectorStore,
    VectorDocument,
    VectorSearchConfig,
    VectorSearchResult,
    VectorStoreType,
    create_vector_store,
)

__all__ = [
    "BaseVectorStore",
    "PGVectorStore",
    "VectorDocument",
    "VectorSearchConfig",
    "VectorSearchResult",
    "VectorStoreType",
    "create_vector_store",
]