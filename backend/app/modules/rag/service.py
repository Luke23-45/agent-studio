"""
RAG (Retrieval Augmented Generation) service.

Handles document retrieval, embedding generation, and context building.
"""

import structlog
from typing import Any
from uuid import UUID

from backend.app.adapters.vectorstore import (
    BaseVectorStore,
    VectorDocument,
    VectorSearchConfig,
    VectorSearchResult,
)
from backend.app.domain.knowledge import KnowledgeDocument, KnowledgeQuery

logger = structlog.get_logger(__name__)


class EmbeddingService:
    """Generates embeddings for documents and queries."""

    def __init__(self, model_name: str = "sentence-transformers/all-MiniLM-L6-v2"):
        self.model_name = model_name
        self._model: Any | None = None

    def _get_model(self) -> Any:
        """Lazy load embedding model."""
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
                self._model = SentenceTransformer(self.model_name)
            except ImportError:
                raise ImportError("sentence-transformers package not installed")
        return self._model

    def embed_text(self, text: str) -> list[float]:
        """Generate embedding for a single text."""
        model = self._get_model()
        embedding = model.encode(text, convert_to_numpy=True)
        return embedding.tolist()

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for multiple texts."""
        model = self._get_model()
        embeddings = model.encode(texts, convert_to_numpy=True)
        return embeddings.tolist()


class RAGService:
    """Main RAG service for retrieval and context building."""

    def __init__(
        self,
        vector_store: BaseVectorStore,
        embedding_service: EmbeddingService | None = None,
        tenant_id: UUID | None = None,
    ):
        self.vector_store = vector_store
        self.embedding_service = embedding_service or EmbeddingService()
        self.tenant_id = tenant_id

    async def add_documents(
        self,
        documents: list[KnowledgeDocument],
    ) -> list[str]:
        """Add documents to the knowledge base."""
        logger.info("adding_documents", count=len(documents), tenant_id=self.tenant_id)

        # Generate embeddings
        texts = [doc.content for doc in documents]
        embeddings = self.embedding_service.embed_texts(texts)

        # Convert to vector documents
        vector_docs = []
        for doc, embedding in zip(documents, embeddings):
            vector_doc = VectorDocument(
                id=doc.id,
                content=doc.content,
                embedding=embedding,
                metadata={
                    "tenant_id": str(self.tenant_id) if self.tenant_id else None,
                    "source": doc.source,
                    "created_at": doc.created_at.isoformat() if doc.created_at else None,
                    **doc.metadata,
                },
            )
            vector_docs.append(vector_doc)

        # Store in vector database
        ids = await self.vector_store.add_documents(vector_docs)
        logger.info("documents_added", count=len(ids))
        return ids

    async def search(
        self,
        query: KnowledgeQuery,
        top_k: int = 5,
        score_threshold: float = 0.0,
    ) -> list[VectorSearchResult]:
        """Search for relevant documents."""
        logger.info(
            "searching_documents",
            query_length=len(query.text),
            top_k=top_k,
            tenant_id=self.tenant_id,
        )

        # Generate query embedding
        query_embedding = self.embedding_service.embed_text(query.text)

        # Build search config
        filter_metadata = {}
        if self.tenant_id:
            filter_metadata["tenant_id"] = str(self.tenant_id)
        if query.filters:
            filter_metadata.update(query.filters)

        search_config = VectorSearchConfig(
            top_k=top_k,
            score_threshold=score_threshold,
            filter_metadata=filter_metadata,
        )

        # Search
        results = await self.vector_store.search(query_embedding, search_config)
        logger.info("search_complete", result_count=len(results))
        return results

    async def retrieve_context(
        self,
        query_text: str,
        top_k: int = 5,
        score_threshold: float = 0.3,
    ) -> str:
        """Retrieve and format context for LLM prompt."""
        query = KnowledgeQuery(text=query_text)
        results = await self.search(query, top_k=top_k, score_threshold=score_threshold)

        if not results:
            return ""

        # Format results as context
        context_parts = []
        for i, result in enumerate(results, 1):
            context_parts.append(
                f"[Source {i}] (Score: {result.score:.2f})\n{result.document.content}"
            )

        context = "\n\n".join(context_parts)
        logger.info("context_retrieved", length=len(context))
        return context

    async def delete_documents(self, ids: list[str]) -> None:
        """Delete documents from the knowledge base."""
        logger.info("deleting_documents", count=len(ids))
        await self.vector_store.delete_documents(ids)

    async def get_document(self, doc_id: str) -> KnowledgeDocument | None:
        """Get a single document by ID."""
        vector_doc = await self.vector_store.get_document(doc_id)
        if vector_doc is None:
            return None

        return KnowledgeDocument(
            id=vector_doc.id,
            content=vector_doc.content,
            source=vector_doc.metadata.get("source", ""),
            metadata=vector_doc.metadata,
        )


def create_rag_service(
    vector_store: BaseVectorStore,
    tenant_id: UUID | None = None,
    embedding_model: str | None = None,
) -> RAGService:
    """Factory function to create RAG service."""
    embedding_service = EmbeddingService(
        model_name=embedding_model or "sentence-transformers/all-MiniLM-L6-v2"
    )
    return RAGService(
        vector_store=vector_store,
        embedding_service=embedding_service,
        tenant_id=tenant_id,
    )
