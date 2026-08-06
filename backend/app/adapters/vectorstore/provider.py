"""
Vector store adapters.

Provides unified interface for different vector databases (pgvector, Qdrant, etc.).
"""

import math
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
    MEMORY = "memory"


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

    async def delete_matching(
        self,
        filter_metadata: dict[str, Any],
        before: float | None = None,
    ) -> int:
        """Delete all documents whose metadata matches every filter pair.

        ``before`` is a unix timestamp: only documents with a numeric
        ``stored_at`` metadata value below the cutoff are deleted.
        Returns the number of deleted documents.
        """
        raise NotImplementedError(f"{type(self).__name__} does not support delete_matching")

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

    async def delete_matching(self, filter_metadata: dict[str, Any], before: float | None = None) -> int:
        """Delete documents whose jsonb metadata matches every filter pair.

        ``before`` filters on the numeric ``stored_at`` metadata value.
        """
        from sqlalchemy import text

        if not filter_metadata:
            raise ValueError("delete_matching requires at least one filter pair")
        pool = await self._get_pool()

        conditions = []
        params: dict[str, Any] = {}
        for idx, (key, value) in enumerate(filter_metadata.items()):
            conditions.append(f"metadata->>:fk_{idx} = :fv_{idx}")
            params[f"fk_{idx}"] = key
            params[f"fv_{idx}"] = str(value)
        if before is not None:
            conditions.append("(metadata->>'stored_at')::double precision < :before")
            params["before"] = float(before)

        query = text(
            f"DELETE FROM {self.table_name} "
            f"WHERE {' AND '.join(conditions)}"
        )

        async with pool.begin() as conn:
            result = await conn.execute(query, params)
            return result.rowcount or 0

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


class InMemoryVectorStore(BaseVectorStore):
    """Single-process in-memory vector store.

    Real implementation (pure-python cosine similarity, metadata filters,
    score thresholds) used for local development, tests, and deployments
    without a Postgres/pgvector backend. Data does not survive restart;
    production should use PGVectorStore.
    """

    def __init__(self, embedding_dim: int = 384):
        self.embedding_dim = embedding_dim
        self._documents: dict[str, VectorDocument] = {}

    @property
    def store_type(self) -> VectorStoreType:
        return VectorStoreType.PGVECTOR  # interface-compatible fallback

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = 0.0
        norm_a = 0.0
        norm_b = 0.0
        for x, y in zip(a, b):
            dot += x * y
            norm_a += x * x
            norm_b += y * y
        if norm_a == 0.0 or norm_b == 0.0:
            return 0.0
        return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))

    def _matches_filter(self, metadata: dict[str, Any], filter_metadata: dict[str, Any]) -> bool:
        for key, expected in filter_metadata.items():
            if str(metadata.get(key)) != str(expected):
                return False
        return True

    async def add_documents(self, documents: list[VectorDocument]) -> list[str]:
        ids: list[str] = []
        for doc in documents:
            self._documents[doc.id] = doc
            ids.append(doc.id)
        return ids

    async def search(
        self, query_embedding: list[float], config: VectorSearchConfig
    ) -> list[VectorSearchResult]:
        scored: list[tuple[float, VectorDocument]] = []
        for doc in self._documents.values():
            if not self._matches_filter(doc.metadata, config.filter_metadata):
                continue
            score = self._cosine_similarity(query_embedding, doc.embedding)
            if score >= config.score_threshold:
                scored.append((score, doc))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [
            VectorSearchResult(document=doc, score=score)
            for score, doc in scored[: config.top_k]
        ]

    async def delete_documents(self, ids: list[str]) -> None:
        for doc_id in ids:
            self._documents.pop(doc_id, None)

    async def delete_matching(self, filter_metadata: dict[str, Any], before: float | None = None) -> int:
        matching = []
        for doc_id, doc in self._documents.items():
            if not self._matches_filter(doc.metadata, filter_metadata):
                continue
            if before is not None:
                stored_at = doc.metadata.get("stored_at")
                if stored_at is None or not isinstance(stored_at, (int, float)) or stored_at >= before:
                    continue
            matching.append(doc_id)
        for doc_id in matching:
            self._documents.pop(doc_id, None)
        return len(matching)

    async def get_document(self, doc_id: str) -> VectorDocument | None:
        return self._documents.get(doc_id)

    def __len__(self) -> int:
        return len(self._documents)


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

    if store_type == VectorStoreType.MEMORY:
        return InMemoryVectorStore(**kwargs)

    if store_type not in stores:
        raise ValueError(f"Unsupported vector store: {store_type}")

    return stores[store_type](connection_string, **kwargs)


def create_vector_store_from_settings() -> BaseVectorStore:
    """Create the vector store configured via VECTOR_STORE settings.

    auto: pgvector when DATABASE_URL points at Postgres, in-memory otherwise
    (the in-memory store is a real implementation; it is simply not durable).
    """
    from backend.app.settings.env import settings

    configured = settings.VECTOR_STORE.strip().lower()
    if configured == "pgvector":
        return create_vector_store(
            VectorStoreType.PGVECTOR,
            settings.DATABASE_URL,
            table_name=settings.VECTOR_STORE_TABLE,
        )
    if configured == "memory":
        return create_vector_store(VectorStoreType.MEMORY, settings.DATABASE_URL)
    if configured == "auto":
        if settings.DATABASE_URL.startswith("postgres"):
            return create_vector_store(
                VectorStoreType.PGVECTOR,
                settings.DATABASE_URL,
                table_name=settings.VECTOR_STORE_TABLE,
            )
        return create_vector_store(VectorStoreType.MEMORY, settings.DATABASE_URL)
    raise ValueError(
        f"Invalid VECTOR_STORE value: {settings.VECTOR_STORE!r}. "
        "Expected one of: auto, pgvector, memory"
    )
