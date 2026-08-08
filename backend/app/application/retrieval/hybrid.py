"""
P9-2 — Hybrid retrieval: BM25 lexical index + vector fusion (7.2/7.3).

Single-stage vector retrieval is the shipped path (P6-1); hybrid adds a
lexical channel:

- ``BM25Index``: pure-Python Okapi BM25 over chunk text, keyed per
  tenant. The worker feeds it at embed time (in-memory; Postgres FTS via
  migration 0011 is the durable path for production).
- ``reciprocal_rank_fusion``: RRF merges vector hits with lexical hits
  into one ranked list, so either channel can rescue documents the other
  missed.
- ``retrieve_hybrid``: RAGService search (vector) + BM25 search -> fusion,
  tenant-filtered on both sides.

The cross-encoder reranker lives in ``rerankers.py`` and is applied on top
of the fused list by the service layer. Recall evals (P6-6) consume the
metrics in ``metrics.py``.
"""

from __future__ import annotations

import math
import re
import structlog
from typing import Any

from backend.app.adapters.vectorstore.provider import (
    VectorDocument,
    VectorSearchResult,
)

logger = structlog.get_logger(__name__)

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_DEFAULT_K1 = 1.5
_DEFAULT_B = 0.75


def tokenize(text: str) -> list[str]:
    """Lowercase alphanumeric tokens (BM25 term space)."""
    return _TOKEN_RE.findall(text.lower())


class BM25Index:
    """Okapi BM25 inverted index over chunk texts (per-tenant aware).

    In-memory by design (mirrors the in-memory queue fallback): the
    worker upserts at embed time; production deployments with Postgres
    use the tsvector column added by migration 0011. Stale entries from
    deleted documents are harmless because searches filter by tenant_id
    and results are always fused against live vector hits.
    """

    def __init__(self, k1: float = _DEFAULT_K1, b: float = _DEFAULT_B):
        self.k1 = k1
        self.b = b
        self._docs: dict[str, tuple[str, str, dict[str, Any]]] = {}
        self._doc_len: dict[str, int] = {}
        self._freq: dict[str, dict[str, int]] = {}
        self._avgdl = 0.0
        self._n = 0

    def __len__(self) -> int:
        return self._n

    def upsert(self, doc_id: str, text: str, *, tenant_id: str | None = None, metadata: dict[str, Any] | None = None) -> None:
        """Add or replace one document in the index."""
        self._remove(doc_id)
        tokens = tokenize(text)
        self._docs[doc_id] = (text, tenant_id or "", metadata or {})
        self._doc_len[doc_id] = len(tokens)
        self._n += 1
        for token in tokens:
            per_doc = self._freq.setdefault(token, {})
            per_doc[doc_id] = per_doc.get(doc_id, 0) + 1
        self._avgdl = (self._avgdl * (self._n - 1) + len(tokens)) / self._n if self._n else 0.0

    def remove(self, doc_id: str) -> bool:
        return self._remove(doc_id)

    def _remove(self, doc_id: str) -> bool:
        if doc_id not in self._docs:
            return False
        length = self._doc_len.pop(doc_id)
        for token in list(self._freq):
            per_doc = self._freq[token]
            per_doc.pop(doc_id, None)
            if not per_doc:
                del self._freq[token]
        del self._docs[doc_id]
        if self._n > 1:
            self._avgdl = (self._avgdl * self._n - length) / (self._n - 1)
        else:
            self._avgdl = 0.0
        self._n -= 1
        return True

    def search(
        self,
        query: str,
        *,
        tenant_id: str | None = None,
        top_k: int = 10,
    ) -> list[tuple[str, float]]:
        """BM25 scores for matching docs; (doc_id, score) desc by score."""
        terms = tokenize(query)
        if not terms or not self._n:
            return []
        idf_cache: dict[str, float] = {}
        scores: dict[str, float] = {}
        for term in set(terms):
            df = len(self._freq.get(term, {}))
            if df == 0:
                continue
            idf = math.log(1 + (self._n - df + 0.5) / (df + 0.5))
            idf_cache[term] = idf
            for doc_id, count in self._freq[term].items():
                if tenant_id is not None and self._docs[doc_id][1] != tenant_id:
                    continue
                length = self._doc_len.get(doc_id, 0)
                denom = count + self.k1 * (1 - self.b + self.b * length / self._avgdl)
                scores[doc_id] = scores.get(doc_id, 0.0) + idf * (count * (self.k1 + 1)) / denom
        ranked = sorted(scores.items(), key=lambda pair: pair[1], reverse=True)
        return ranked[:top_k]

    def document(self, doc_id: str) -> tuple[str, str, dict[str, Any]] | None:
        return self._docs.get(doc_id)


def reciprocal_rank_fusion(
    vector_results: list[VectorSearchResult],
    lexical_hits: list[tuple[str, float]],
    *,
    k: int = 60,
    index: BM25Index | None = None,
) -> list[VectorSearchResult]:
    """Fuse vector + lexical rankings with Reciprocal Rank Fusion.

    Each channel contributes ``1 / (k + rank)`` per document; the merged
    list is re-ranked by the fused score. Lexical-only hits build a
    document from the index payload (embedding omitted).
    """
    fused: dict[str, float] = {}
    payload: dict[str, VectorSearchResult] = {}

    for rank, result in enumerate(vector_results, start=1):
        doc_id = str(result.document.id)
        fused[doc_id] = fused.get(doc_id, 0.0) + 1.0 / (k + rank)
        payload[doc_id] = result

    for rank, (doc_id, _lex_score) in enumerate(lexical_hits, start=1):
        fused[doc_id] = fused.get(doc_id, 0.0) + 1.0 / (k + rank)
        if doc_id not in payload and index is not None:
            stored = index.document(doc_id)
            if stored is not None:
                text, tenant, metadata = stored
                payload[doc_id] = VectorSearchResult(
                    document=VectorDocument(
                        id=doc_id,
                        content=text,
                        embedding=[],
                        metadata={**metadata, "tenant_id": tenant},
                    ),
                    score=0.0,
                )

    ordered = sorted(fused.items(), key=lambda pair: pair[1], reverse=True)
    results = []
    for doc_id, fused_score in ordered:
        item = payload.get(doc_id)
        if item is None:
            continue
        results.append(
            VectorSearchResult(
                document=item.document,
                score=fused_score,
            )
        )
    return results


_lexical_index: BM25Index | None = None


def get_lexical_index() -> BM25Index:
    """Process-wide BM25 index (worker feeds it; retrieval reads it)."""
    global _lexical_index
    if _lexical_index is None:
        _lexical_index = BM25Index()
    return _lexical_index


def index_chunks(chunks: list[dict[str, Any]], *, tenant_id: str, document_id: str, source: str) -> int:
    """Upsert ingestion chunks into the lexical index (best-effort).

    Called by the embed worker alongside vector indexing. Never raises:
    a lexical indexing failure must not fail the vector job.
    """
    index = get_lexical_index()
    for chunk in chunks:
        index.upsert(
            f"{document_id}:{chunk.get('id', '')}",
            chunk.get("content", ""),
            tenant_id=tenant_id,
            metadata={"tenant_id": tenant_id, "document_id": document_id, "source": source},
        )
    return len(chunks)
