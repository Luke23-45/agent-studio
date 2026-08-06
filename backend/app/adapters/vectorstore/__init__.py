"""
Vector store adapters.

Provides unified interface for different vector databases (pgvector, Qdrant, etc.).
"""

from .provider import (
    BaseVectorStore,
    InMemoryVectorStore,
    PGVectorStore,
    VectorDocument,
    VectorSearchConfig,
    VectorSearchResult,
    VectorStoreType,
    create_vector_store,
    create_vector_store_from_settings,
)

__all__ = [
    "BaseVectorStore",
    "InMemoryVectorStore",
    "PGVectorStore",
    "VectorDocument",
    "VectorSearchConfig",
    "VectorSearchResult",
    "VectorStoreType",
    "create_vector_store",
    "create_vector_store_from_settings",
]
