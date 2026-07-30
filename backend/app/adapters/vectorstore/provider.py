"""
Vector store adapters.

Provides unified interface for different vector databases (pgvector, Qdrant, etc.).
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class VectorStoreType(Enum):
    """Supported vector store types."""

    PGVECTOR = "pgvector"
    PGVECTORSCALE = "pgvectorscale"
    QDRANT = "qdrant"
    PINECONE = "pinecone"


@dataclass
class VectorDocument:
    """A document with embeddings in the vector store."""

    id: str
    content: str
    embedding: list[float]
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class VectorSearchResult:
    """Result from a vector search."""

    document: VectorDocument
    score: float


@dataclass
class VectorSearchConfig:
    """Configuration for vector search."""

    top_k: int = 5
    score_threshold: float = 0.0
    filter_metadata: dict[str, Any] = field(default_factory=dict)


class BaseVectorStore(ABC):
    """Base adapter for vector stores."""

    @abstractmethod
    async def add_documents(self, documents: list[VectorDocument]) -> list[str]:
        """Add documents to the vector store. Returns IDs."""
        pass

    @abstractmethod
    async def search(
        self, query_embedding: list[float], config: VectorSearchConfig
    ) -> list[VectorSearchResult]:
        """Search for similar documents."""
        pass

    @abstractmethod
    async def delete_documents(self, ids: list[str]) -> None:
        """Delete documents by ID."""
        pass

    @abstractmethod
    async def get_document(self, doc_id: str) -> VectorDocument | None:
        """Get a single document by ID."""
        pass

    @property
    @abstractmethod
    def store_type(self) -> VectorStoreType:
        """Return the store type."""
        pass


class PGVectorStore(BaseVectorStore):
    """PostgreSQL + pgvector implementation."""

    def __init__(
        self,
        connection_string: str,
        table_name: str = "embeddings",
        embedding_dim: int = 1536,
    ):
        self.connection_string = connection_string
        self.table_name = table_name
        self.embedding_dim = embedding_dim
        self._pool: Any | None = None

    @property
    def store_type(self) -> VectorStoreType:
        return VectorStoreType.PGVECTOR

    async def _get_pool(self) -> Any:
        """Lazy load database connection pool."""
        if self._pool is None:
            try:
                from sqlalchemy.ext.asyncio import create_async_engine
                from sqlalchemy.pool import NullPool

                engine = create_async_engine(
                    self.connection_string,
                    poolclass=NullPool,
                    echo=False,
                )
                self._pool = engine
            except ImportError:
                raise ImportError("sqlalchemy package not installed")
        return self._pool

    async def add_documents(self, documents: list[VectorDocument]) -> list[str]:
        """Add documents to pgvector."""
        from sqlalchemy import text

        pool = await self._get_pool()
        ids = []

        async with pool.begin() as conn:
            for doc in documents:
                # Convert embedding to PostgreSQL array format
                embedding_str = "[" + ",".join(str(v) for v in doc.embedding) + "]"

                query = text(f"""
                    INSERT INTO {self.table_name} (id, content, embedding, metadata)
                    VALUES (:id, :content, :embedding::vector, :metadata::jsonb)
                    ON CONFLICT (id) DO UPDATE SET
                        content = EXCLUDED.content,
                        embedding = EXCLUDED.embedding,
                        metadata = EXCLUDED.metadata
                    RETURNING id
                """)

                result = await conn.execute(
                    query,
                    {
                        "id": doc.id,
                        "content": doc.content,
                        "embedding": embedding_str,
                        "metadata": doc.metadata,
                    },
                )
                row = result.fetchone()
                if row:
                    ids.append(row[0])

        return ids

    async def search(
        self, query_embedding: list[float], config: VectorSearchConfig
    ) -> list[VectorSearchResult]:
        """Search using cosine similarity."""
        from sqlalchemy import text

        pool = await self._get_pool()
        embedding_str = "[" + ",".join(str(v) for v in query_embedding) + "]"

        # Build filter conditions
        filter_conditions = []
        filter_params = {}
        for key, value in config.filter_metadata.items():
            filter_conditions.append(f"metadata->>:filter_{key} = :filter_val_{key}")
            filter_params[f"filter_{key}"] = key
            filter_params[f"filter_val_{key}"] = str(value)

        where_clause = ""
        if filter_conditions:
            where_clause = "WHERE " + " AND ".join(filter_conditions)

        query = text(f"""
            SELECT id, content, embedding, metadata,
                   1 - (embedding <=> :query_embedding::vector) AS similarity
            FROM {self.table_name}
            {where_clause}
            ORDER BY embedding <=> :query_embedding::vector
            LIMIT :limit
        """)

        params = {
            "query_embedding": embedding_str,
            "limit": config.top_k,
            **filter_params,
        }

        results = []
        async with pool.begin() as conn:
            result = await conn.execute(query, params)
            rows = result.fetchall()

            for row in rows:
                if row.similarity >= config.score_threshold:
                    doc = VectorDocument(
                        id=row.id,
                        content=row.content,
                        embedding=list(row.embedding) if row.embedding else [],
                        metadata=row.metadata or {},
                    )
                    results.append(VectorSearchResult(document=doc, score=row.similarity))

        return results

    async def delete_documents(self, ids: list[str]) -> None:
        """Delete documents by ID."""
        from sqlalchemy import text

        pool = await self._get_pool()
        query = text(f"DELETE FROM {self.table_name} WHERE id = ANY(:ids)")

        async with pool.begin() as conn:
            await conn.execute(query, {"ids": ids})

    async def get_document(self, doc_id: str) -> VectorDocument | None:
        """Get a single document."""
        from sqlalchemy import text

        pool = await self._get_pool()
        query = text(f"""
            SELECT id, content, embedding, metadata
            FROM {self.table_name}
            WHERE id = :id
        """)

        async with pool.begin() as conn:
            result = await conn.execute(query, {"id": doc_id})
            row = result.fetchone()

            if row:
                return VectorDocument(
                    id=row.id,
                    content=row.content,
                    embedding=list(row.embedding) if row.embedding else [],
                    metadata=row.metadata or {},
                )
            return None


def create_vector_store(
    store_type: VectorStoreType,
    connection_string: str,
    **kwargs: Any,
) -> BaseVectorStore:
    """Factory function to create appropriate vector store."""
    stores = {
        VectorStoreType.PGVECTOR: PGVectorStore,
        VectorStoreType.PGVECTORSCALE: PGVectorStore,  # Same interface
    }

    if store_type not in stores:
        raise ValueError(f"Unsupported vector store: {store_type}")

    return stores[store_type](connection_string, **kwargs)
