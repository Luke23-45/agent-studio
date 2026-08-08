"""
P9-2 — Rerankers for the retrieval path (7.3).

Two implementations of the ``Reranker`` protocol:

- ``CrossEncoderReranker``: sentence-transformers cross-encoder over
  ``(query, candidate)`` pairs. Lazy-loaded (heavy model); the
  ENABLE_CROSS_ENCODER flag must be on, and score failures degrade to
  the input order (a reranker must never break retrieval).
- ``ScoreFusionReranker``: the classic lexical-overlap re-score
  (vector score * 0.7 + query-term overlap * 0.3) -- the P6 single-stage
  fallback, now behind the same interface.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import Any

import structlog

from backend.app.adapters.vectorstore.provider import VectorSearchResult

logger = structlog.get_logger(__name__)


class Reranker(ABC):
    """Re-scores candidate documents against the query."""

    @abstractmethod
    async def score_batch(self, query: str, texts: list[str]) -> list[float]:
        """One relevance score per text (higher = more relevant)."""


class CrossEncoderReranker(Reranker):
    """Cross-encoder scoring (lazy model load, executor-bound)."""

    def __init__(self, model_name: str | None = None):
        from backend.app.settings.env import get_settings

        self.model_name = model_name or get_settings().CROSS_ENCODER_MODEL
        self._model: Any | None = None

    def _load(self) -> Any:
        if self._model is not None:
            return self._model
        from sentence_transformers import CrossEncoder

        self._model = CrossEncoder(self.model_name)
        return self._model

    async def score_batch(self, query: str, texts: list[str]) -> list[float]:
        if not texts:
            return []
        loop = asyncio.get_event_loop()

        def _run() -> list[float]:
            model = self._load()
            pairs = [(query, text) for text in texts]
            scores = model.predict(pairs, show_progress_bar=False)
            return [float(s) for s in scores]

        try:
            return await loop.run_in_executor(None, _run)
        except Exception as e:  # pragma: no cover - degrade, never break
            logger.warning("cross_encoder_failed_degrading", error=str(e))
            return [0.0] * len(texts)


class ScoreFusionReranker(Reranker):
    """Lexical-overlap re-score (the P6 single-stage fallback)."""

    def __init__(self, vector_weight: float = 0.7, lexical_weight: float = 0.3):
        self.vector_weight = vector_weight
        self.lexical_weight = lexical_weight

    async def score_batch(self, query: str, texts: list[str]) -> list[float]:
        query_terms = set(query.lower().split())
        if not query_terms:
            return [1.0] * len(texts)
        scores = []
        for text in texts:
            content_terms = set(text.lower().split())
            overlap = len(query_terms & content_terms)
            keyword_score = overlap / len(query_terms)
            scores.append(keyword_score)
        return scores


async def apply_rerank(
    results: list[VectorSearchResult],
    query: str,
    reranker: Reranker | None,
) -> list[VectorSearchResult]:
    """Re-order results by the reranker; failures preserve input order."""
    if not results or reranker is None:
        return results
    try:
        scores = await reranker.score_batch(query, [r.document.content for r in results])
        if len(scores) != len(results):
            logger.warning("reranker_score_count_mismatch", expected=len(results), got=len(scores))
            return results
        return [r for r, _ in sorted(zip(results, scores), key=lambda p: p[1], reverse=True)]
    except Exception as e:  # noqa: BLE001 - reranking never breaks retrieval
        logger.warning("rerank_failed_preserving_order", error=str(e))
        return results


def score_fusion_rerank(
    results: list[VectorSearchResult],
    query: str,
    *,
    vector_weight: float = 0.7,
    lexical_weight: float = 0.3,
) -> list[VectorSearchResult]:
    """Sync legacy helper (kept for callers of ``_simple_rerank``)."""

    def score(r: VectorSearchResult) -> float:
        query_terms = set(query.lower().split())
        if not query_terms:
            return r.score
        content_terms = set(r.document.content.lower().split())
        overlap = len(query_terms & content_terms)
        keyword_score = overlap / len(query_terms)
        return r.score * vector_weight + keyword_score * lexical_weight

    return sorted(results, key=score, reverse=True)
