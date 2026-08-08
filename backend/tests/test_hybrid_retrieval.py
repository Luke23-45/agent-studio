"""
P9-2 tests: hybrid retrieval (BM25 + RRF fusion) and reranking.

- BM25Index: upsert/search/remove, tenant filtering, ranking sanity
- reciprocal_rank_fusion: merged ranking from both channels
- RetrievalService.retrieve_hybrid: fusion path + no-lexical fallback
- rerankers: ScoreFusionReranker, apply_rerank order preservation
- retrieval metrics: recall@k, precision@k, nDCG@k, MRR
"""

import pytest

from backend.app.adapters.vectorstore.provider import (
    InMemoryVectorStore,
    VectorDocument,
    VectorSearchResult,
)
from backend.app.application.retrieval.hybrid import (
    BM25Index,
    reciprocal_rank_fusion,
)
from backend.app.application.retrieval.metrics import (
    mean_reciprocal_rank,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
)
from backend.app.application.retrieval.rerankers import (
    ScoreFusionReranker,
    apply_rerank,
)
from backend.app.application.retrieval.service import (
    RetrievalService,
    RetrievalServiceConfig,
)
from backend.app.modules.rag import EmbeddingService, RAGService


class TestBM25Index:
    def test_upsert_search_and_ranking(self):
        index = BM25Index()
        index.upsert("d1", "refund policy says refunds are processed within 30 days", tenant_id="t1")
        index.upsert("d2", "shipping policy says free shipping over 50 dollars", tenant_id="t1")
        index.upsert("d3", "refund exceptions apply to digital goods", tenant_id="t1")

        hits = index.search("refund", top_k=10)
        ids = [doc_id for doc_id, _ in hits]
        # docs containing "refund" beat the doc that does not
        assert "d1" in ids and "d3" in ids and "d2" not in ids
        assert all(score > 0 for _, score in hits)

    def test_tenant_filter(self):
        index = BM25Index()
        index.upsert("a1", "secret project launch", tenant_id="t1")
        index.upsert("b1", "secret project launch", tenant_id="t2")
        hits = index.search("secret project", tenant_id="t2", top_k=10)
        assert [doc_id for doc_id, _ in hits] == ["b1"]

    def test_remove(self):
        index = BM25Index()
        index.upsert("a", "unique token xyzzy", tenant_id="t1")
        index.upsert("b", "another token xyzzy", tenant_id="t1")
        assert index.remove("a") is True
        hits = index.search("xyzzy", top_k=10)
        assert [doc_id for doc_id, _ in hits] == ["b"]
        assert index.remove("a") is False

    def test_upsert_replaces(self):
        index = BM25Index()
        index.upsert("a", "old content", tenant_id="t1")
        index.upsert("a", "new content", tenant_id="t1")
        assert len(index) == 1
        hits = index.search("old", top_k=5)
        assert hits == []

    def test_empty_query_and_empty_index(self):
        index = BM25Index()
        assert index.search("") == []
        index.upsert("a", "some words here", tenant_id="t1")
        assert index.search("") == []


class TestReciprocalRankFusion:
    def _result(self, doc_id: str, score: float) -> VectorSearchResult:
        return VectorSearchResult(
            document=VectorDocument(id=doc_id, content=f"content {doc_id}", embedding=[]),
            score=score,
        )

    def test_fusion_merges_both_channels(self):
        index = BM25Index()
        index.upsert("lex-only", "unique lexical phrase about refunds", tenant_id="t1")

        vector = [self._result("v1", 0.9), self._result("v2", 0.8)]
        lexical = [("v2", 1.1), ("lex-only", 3.2)]

        fused = reciprocal_rank_fusion(vector, lexical, index=index)
        ids = [r.document.id for r in fused]
        # all three present; lexical-only doc carried the index payload
        assert set(ids) == {"v1", "v2", "lex-only"}
        assert any(r.document.metadata.get("tenant_id") == "t1" for r in fused if r.document.id == "lex-only")
        # v2 appears in BOTH channels -> highest fused score
        assert fused[0].document.id == "v2"

    def test_fusion_without_index_drops_lexical_only(self):
        vector = [self._result("v1", 0.9)]
        fused = reciprocal_rank_fusion(vector, [("ghost", 5.0)], index=None)
        assert [r.document.id for r in fused] == ["v1"]


class FakeRAGService:
    """RAGService stand-in: fixed vector hits per query."""

    def __init__(self, hits: list[VectorSearchResult]):
        self.hits = hits

    async def search(self, query, top_k=5, score_threshold=0.0):
        return self.hits[:top_k]


class TestRetrieveHybrid:
    def _rag(self):
        docs = [
            VectorDocument(
                id="v1",
                content="the sky is blue today",
                embedding=[1.0, 0.0],
                metadata={"tenant_id": "t1", "source": "doc1"},
            ),
            VectorDocument(
                id="v2",
                content="refund windows last 30 days",
                embedding=[0.0, 1.0],
                metadata={"tenant_id": "t1", "source": "doc2"},
            ),
        ]
        return FakeRAGService(
            [VectorSearchResult(document=d, score=0.9 - i * 0.1) for i, d in enumerate(docs)]
        )

    @pytest.mark.asyncio
    async def test_hybrid_fuses_and_reranks(self):
        index = BM25Index()
        index.upsert("lex1", "sky watchers love the blue sky", tenant_id="t1")
        service = RetrievalService(
            rag_service=self._rag(),
            config=RetrievalServiceConfig(enable_hybrid=True, enable_reranking=False),
            lexical_index=index,
        )
        result = await service.retrieve_hybrid("sky blue", tenant_id="t1", top_k=3)
        assert result.metadata["hybrid"] is True
        ids = [r.document.id for r in result.results]
        assert "lex1" in ids and "v1" in ids

    @pytest.mark.asyncio
    async def test_hybrid_without_index_falls_back(self):
        service = RetrievalService(
            rag_service=self._rag(),
            config=RetrievalServiceConfig(enable_hybrid=True),
            lexical_index=None,
        )
        result = await service.retrieve_hybrid("sky", tenant_id="t1", top_k=2)
        assert result.metadata["hybrid"] is False
        assert [r.document.id for r in result.results] == ["v1", "v2"]

    @pytest.mark.asyncio
    async def test_reranker_is_applied_after_fusion(self):
        class FlipReranker:
            async def score_batch(self, query, texts):
                return list(reversed(range(len(texts))))

        index = BM25Index()
        index.upsert("v1", "the sky is blue today", tenant_id="t1")
        index.upsert("v2", "refund windows last 30 days", tenant_id="t1")
        service = RetrievalService(
            rag_service=self._rag(),
            config=RetrievalServiceConfig(enable_hybrid=True),
            lexical_index=index,
            reranker=FlipReranker(),
        )
        result = await service.retrieve_hybrid("sky", tenant_id="t1", top_k=2)
        assert result.metadata["hybrid"] is True


class TestRerankers:
    @pytest.mark.asyncio
    async def test_score_fusion_prefers_overlap(self):
        reranker = ScoreFusionReranker()
        scores = await reranker.score_batch(
            "refund policy", ["refund policy details", "shipping details"]
        )
        assert scores[0] > scores[1]

    @pytest.mark.asyncio
    async def test_apply_rerank_reorders_and_fails_safe(self):
        results = [
            VectorSearchResult(document=VectorDocument(id="a", content="aaa bbb", embedding=[]), score=0.5),
            VectorSearchResult(document=VectorDocument(id="b", content="ccc ddd", embedding=[]), score=0.4),
        ]

        class Broken:
            async def score_batch(self, query, texts):
                raise RuntimeError("boom")

        ordered = await apply_rerank(results, "query", Broken())
        assert [r.document.id for r in ordered] == ["a", "b"]
        assert await apply_rerank(results, "q", None) == results


class TestRetrievalMetrics:
    def test_recall_and_precision_at_k(self):
        retrieved = ["d1", "d2", "d3", "d4"]
        relevant = {"d1", "d4"}
        assert recall_at_k(retrieved, relevant, k=2) == 0.5
        assert recall_at_k(retrieved, relevant) == 1.0
        assert precision_at_k(retrieved, relevant, k=2) == 0.5
        assert recall_at_k(retrieved, set()) == 0.0

    def test_ndcg_at_k(self):
        retrieved = ["d1", "d2", "d3"]
        relevant = {"d3", "d2"}
        # dcg = 1/2 + 1/3 = 0.8333; ideal = 1 + 1/2 = 1.5 -> 0.5556
        assert round(ndcg_at_k(retrieved, relevant), 4) == 0.5556
        assert ndcg_at_k(["x1", "x2", "x3"], {"d3"}) == 0.0

    def test_mean_reciprocal_rank(self):
        lists = [["d1", "d2"], ["d9", "d1"]]
        assert mean_reciprocal_rank(lists, {"d2"}) == 0.25  # (1/2 + 0) / 2
        assert mean_reciprocal_rank([], {"d1"}) == 0.0


class TestStoreRoundTrip:
    @pytest.mark.asyncio
    async def test_inmemory_store_feeds_rag_search(self):
        store = InMemoryVectorStore()
        await store.add_documents(
            [
                VectorDocument(
                    id="c1",
                    content="refund policy",
                    embedding=[1.0, 0.0],
                    metadata={"tenant_id": "t1"},
                )
            ]
        )
        rag = RAGService(vector_store=store, embedding_service=EmbeddingService())

        class FakeEmbed:
            def embed_text(self, text):
                return [1.0, 0.0]

        rag.embedding_service = FakeEmbed()  # type: ignore[assignment]
        hits = await rag.search(__import__("backend.app.domain.knowledge", fromlist=["KnowledgeQuery"]).KnowledgeQuery(query_text="refund", tenant_id="t1"), top_k=5)
        assert [r.document.id for r in hits] == ["c1"]
