"""
Retrieval service for knowledge base queries.

Handles semantic search, filtering, and context building for RAG workflows.
"""

import structlog
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from backend.app.domain.knowledge import KnowledgeQuery
from backend.app.modules.rag import RAGService, VectorSearchResult

logger = structlog.get_logger(__name__)


@dataclass
class RetrievalResult:
    """Result from a retrieval operation."""

    query: str
    results: list[VectorSearchResult]
    context: str
    total_results: int
    filtered_count: int
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class RetrievalConfig:
    """Configuration for retrieval operations."""

    top_k: int = 5
    score_threshold: float = 0.3
    apply_spotlighting: bool = True
    filter_by_tenant: bool = True
    max_context_length: int = 4096


class RetrievalService:
    """Service for retrieving knowledge base context."""

    def __init__(
        self,
        rag_service: RAGService,
        config: RetrievalConfig | None = None,
    ):
        self.rag_service = rag_service
        self.config = config or RetrievalConfig()

    async def retrieve(
        self,
        query_text: str,
        tenant_id: UUID | None = None,
        filters: dict[str, Any] | None = None,
        custom_config: RetrievalConfig | None = None,
    ) -> RetrievalResult:
        """Retrieve relevant documents for a query."""
        logger.info(
            "retrieving_documents",
            query_length=len(query_text),
            tenant_id=tenant_id,
        )

        config = custom_config or self.config
        knowledge_query = KnowledgeQuery(
            text=query_text,
            filters=filters,
        )

        # Search for documents
        results = await self.rag_service.search(
            query=knowledge_query,
            top_k=config.top_k,
            score_threshold=config.score_threshold,
        )

        # Apply tenant filtering if enabled
        filtered_results = results
        if config.filter_by_tenant and tenant_id:
            filtered_results = [
                r for r in results
                if r.document.metadata.get("tenant_id") == str(tenant_id)
            ]
            if len(filtered_results) < len(results):
                logger.info(
                    "tenant_filter_applied",
                    original_count=len(results),
                    filtered_count=len(filtered_results),
                )

        # Build context with spotlighting
        context = self._build_context(filtered_results, config.apply_spotlighting)

        retrieval_result = RetrievalResult(
            query=query_text,
            results=filtered_results,
            context=context,
            total_results=len(results),
            filtered_count=len(filtered_results),
            metadata={
                "score_threshold": config.score_threshold,
                "top_k": config.top_k,
            },
        )

        logger.info(
            "retrieval_complete",
            result_count=len(filtered_results),
            context_length=len(context),
        )

        return retrieval_result

    def _build_context(
        self,
        results: list[VectorSearchResult],
        apply_spotlighting: bool = True,
    ) -> str:
        """Build formatted context from retrieval results."""
        if not results:
            return ""

        context_parts = []
        
        for i, result in enumerate(results, 1):
            source_metadata = result.document.metadata.get("source", "Unknown")
            score = result.score
            
            if apply_spotlighting:
                # Use spotlighting delimiters to mark retrieved content
                context_part = (
                    f"<<BEGIN_SOURCE_{i}>>\n"
                    f"[Source {i}: {source_metadata}] (Relevance Score: {score:.2f})\n"
                    f"{result.document.content}\n"
                    f"<<END_SOURCE_{i}>>"
                )
            else:
                context_part = (
                    f"[Source {i}: {source_metadata}] (Score: {score:.2f})\n"
                    f"{result.document.content}"
                )
            
            context_parts.append(context_part)

        context = "\n\n".join(context_parts)
        return context

    async def retrieve_with_reranking(
        self,
        query_text: str,
        tenant_id: UUID | None = None,
        top_k: int = 5,
    ) -> RetrievalResult:
        """Retrieve and rerank results for better relevance."""
        # First pass: retrieve more candidates
        initial_config = RetrievalConfig(
            top_k=top_k * 3,  # Get more candidates for reranking
            score_threshold=0.2,  # Lower threshold for first pass
        )
        
        initial_result = await self.retrieve(
            query_text=query_text,
            tenant_id=tenant_id,
            custom_config=initial_config,
        )

        # Simple reranking based on keyword overlap (can be replaced with cross-encoder)
        reranked_results = self._simple_rerank(initial_result.results, query_text)
        
        # Take top_k after reranking
        final_results = reranked_results[:top_k]
        
        # Rebuild context
        context = self._build_context(final_results)

        return RetrievalResult(
            query=query_text,
            results=final_results,
            context=context,
            total_results=initial_result.total_results,
            filtered_count=len(final_results),
            metadata={"reranked": True},
        )

    def _simple_rerank(
        self,
        results: list[VectorSearchResult],
        query: str,
    ) -> list[VectorSearchResult]:
        """Simple reranking based on keyword overlap."""
        query_terms = set(query.lower().split())
        
        def score_overlap(result: VectorSearchResult) -> float:
            content_terms = set(result.document.content.lower().split())
            overlap = len(query_terms & content_terms)
            # Combine with original score
            return result.score * 0.7 + (overlap / len(query_terms)) * 0.3
        
        return sorted(results, key=score_overlap, reverse=True)


def create_retrieval_service(
    rag_service: RAGService,
    top_k: int = 5,
    score_threshold: float = 0.3,
    apply_spotlighting: bool = True,
) -> RetrievalService:
    """Factory function to create retrieval service."""
    config = RetrievalConfig(
        top_k=top_k,
        score_threshold=score_threshold,
        apply_spotlighting=apply_spotlighting,
    )
    return RetrievalService(rag_service=rag_service, config=config)