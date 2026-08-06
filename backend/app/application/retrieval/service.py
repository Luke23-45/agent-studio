import structlog
import time as time_module
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple
from uuid import UUID

from ...domain.knowledge import KnowledgeQuery
from ...modules.rag import RAGService, VectorSearchResult
from ...infrastructure.patterns import ManagedService, HealthComponent, HealthStatus
from .models import RetrievalResult, RetrievalConfig

logger = structlog.get_logger(__name__)


@dataclass
class RetrievalServiceConfig:
    top_k: int = 5
    score_threshold: float = 0.3
    apply_spotlighting: bool = True
    filter_by_tenant: bool = True
    max_context_length: int = 4096
    enable_reranking: bool = True
    enable_query_expansion: bool = False
    cache_ttl_seconds: int = 300
    max_retrieval_time_ms: float = 5000.0


class RetrievalService(ManagedService):
    def __init__(self, rag_service: RAGService, config: Optional[RetrievalServiceConfig] = None):
        super().__init__("retrieval_service")
        self.rag_service = rag_service
        self.config = config or RetrievalServiceConfig()
        self._cache: Dict[str, Tuple[RetrievalResult, float]] = {}

    async def _do_initialize(self) -> None:
        logger.info("retrieval_service_initialized",
                    top_k=self.config.top_k,
                    reranking=self.config.enable_reranking)

    async def _do_close(self) -> None:
        self._cache.clear()

    async def retrieve(
        self,
        query_text: str,
        tenant_id: Optional[UUID] = None,
        filters: Optional[Dict[str, Any]] = None,
        custom_config: Optional[RetrievalConfig] = None,
        skip_cache: bool = False,
        allowed_sources: Optional[List[str]] = None,
    ) -> RetrievalResult:
        start = time_module.time()

        if not query_text or not query_text.strip():
            return RetrievalResult(query=query_text, results=[], context="", total_results=0, filtered_count=0)

        cache_key = f"{tenant_id}:{query_text.strip()}"
        if not skip_cache and cache_key in self._cache:
            cached_result, cached_at = self._cache[cache_key]
            if time_module.time() - cached_at < self.config.cache_ttl_seconds:
                logger.info("cache_hit", query_length=len(query_text), tenant_id=tenant_id)
                return cached_result

        config = custom_config or RetrievalConfig(
            top_k=self.config.top_k,
            score_threshold=self.config.score_threshold,
            apply_spotlighting=self.config.apply_spotlighting,
            filter_by_tenant=self.config.filter_by_tenant,
            max_context_length=self.config.max_context_length,
        )

        knowledge_query = KnowledgeQuery(
            query_text=query_text,
            filters=filters,
            tenant_id=tenant_id,
            allowed_sources=allowed_sources or [],
        )
        results = await self.rag_service.search(
            query=knowledge_query,
            top_k=config.top_k,
            score_threshold=config.score_threshold,
        )

        filtered_results = results
        if config.filter_by_tenant and tenant_id:
            filtered_results = [
                r for r in results
                if r.document.metadata.get("tenant_id") == str(tenant_id)
            ]

        if config.apply_spotlighting and filtered_results:
            context = self._build_spotlight_context(filtered_results)
        else:
            context = self._build_context(filtered_results)

        if config.max_context_length and len(context) > config.max_context_length:
            context = context[:config.max_context_length] + "\n... [truncated]"

        result = RetrievalResult(
            query=query_text,
            results=filtered_results,
            context=context,
            total_results=len(results),
            filtered_count=len(filtered_results),
            metadata={
                "score_threshold": config.score_threshold,
                "top_k": config.top_k,
                "latency_ms": (time_module.time() - start) * 1000,
            },
        )

        self._cache[cache_key] = (result, time_module.time())
        logger.info("retrieval_complete",
                    result_count=len(filtered_results),
                    context_length=len(context),
                    latency_ms=result.metadata["latency_ms"])

        return result

    async def retrieve_with_reranking(
        self,
        query_text: str,
        tenant_id: Optional[UUID] = None,
        top_k: int = 5,
    ) -> RetrievalResult:
        initial = RetrievalConfig(top_k=top_k * 3, score_threshold=0.2)
        initial_result = await self.retrieve(
            query_text=query_text,
            tenant_id=tenant_id,
            custom_config=initial,
        )

        reranked = self._simple_rerank(initial_result.results, query_text)
        final_results = reranked[:top_k]
        context = self._build_spotlight_context(final_results)

        return RetrievalResult(
            query=query_text,
            results=final_results,
            context=context,
            total_results=initial_result.total_results,
            filtered_count=len(final_results),
            metadata={"reranked": True, "candidates_evaluated": len(reranked)},
        )

    async def multi_query_retrieval(
        self,
        query_text: str,
        tenant_id: Optional[UUID] = None,
        query_variations: Optional[List[str]] = None,
        top_k: int = 5,
    ) -> RetrievalResult:
        queries = [query_text] + (query_variations or [])
        all_results: List[VectorSearchResult] = []
        seen_ids: Set[str] = set()

        for q in queries:
            result = await self.retrieve(query_text=q, tenant_id=tenant_id, skip_cache=True)
            for r in result.results:
                doc_id = str(r.document.id)
                if doc_id not in seen_ids:
                    seen_ids.add(doc_id)
                    all_results.append(r)

        all_results.sort(key=lambda r: r.score, reverse=True)
        final_results = all_results[:top_k]
        context = self._build_spotlight_context(final_results)

        return RetrievalResult(
            query=query_text,
            results=final_results,
            context=context,
            total_results=len(all_results),
            filtered_count=len(final_results),
            metadata={"multi_query": True, "variations": len(queries)},
        )

    def _build_context(self, results: List[VectorSearchResult]) -> str:
        if not results:
            return ""
        parts = []
        for i, r in enumerate(results, 1):
            source = r.document.metadata.get("source", "Unknown")
            parts.append(f"[Source {i}: {source}] (Score: {r.score:.2f})\n{r.document.content}")
        return "\n\n".join(parts)

    def _build_spotlight_context(self, results: List[VectorSearchResult]) -> str:
        if not results:
            return ""
        parts = []
        for i, r in enumerate(results, 1):
            source = r.document.metadata.get("source", "Unknown")
            parts.append(
                f"<<BEGIN_SOURCE_{i}>>\n"
                f"[Source {i}: {source}, Relevance: {r.score:.2f}]\n"
                f"{r.document.content}\n"
                f"<<END_SOURCE_{i}>>"
            )
        return "\n\n".join(parts)

    def _simple_rerank(self, results: List[VectorSearchResult], query: str) -> List[VectorSearchResult]:
        query_terms = set(query.lower().split())
        if not query_terms:
            return results

        def score(r: VectorSearchResult) -> float:
            content_terms = set(r.document.content.lower().split())
            overlap = len(query_terms & content_terms)
            keyword_score = overlap / len(query_terms)
            return r.score * 0.7 + keyword_score * 0.3

        return sorted(results, key=score, reverse=True)

    def invalidate_cache(self, tenant_id: Optional[UUID] = None) -> int:
        if tenant_id:
            prefix = f"{tenant_id}:"
            before = len(self._cache)
            self._cache = {k: v for k, v in self._cache.items() if not k.startswith(prefix)}
            return before - len(self._cache)
        else:
            count = len(self._cache)
            self._cache.clear()
            return count

    async def _do_health_check(self) -> HealthComponent:
        return HealthComponent(
            name=self.name,
            status=HealthStatus.HEALTHY,
            metadata={
                "cache_size": len(self._cache),
                "top_k": self.config.top_k,
                "reranking": self.config.enable_reranking,
            },
        )


def create_retrieval_service(
    rag_service: RAGService,
    top_k: int = 5,
    score_threshold: float = 0.3,
) -> RetrievalService:
    config = RetrievalServiceConfig(top_k=top_k, score_threshold=score_threshold)
    return RetrievalService(rag_service=rag_service, config=config)
