"""
P9-2 — Retrieval quality metrics for the recall evals (P6-6).

Standard ranked-retrieval metrics over id lists; the config-version eval
gate and the ``evals/`` harness consume these to decide when hybrid
retrieval + reranking are worth enabling for a tenant.
"""

from __future__ import annotations


def recall_at_k(retrieved_ids: list[str], relevant_ids: set[str], k: int | None = None) -> float:
    """Fraction of relevant documents found in the top-k retrieval."""
    if not relevant_ids:
        return 0.0
    if k is not None:
        retrieved_ids = retrieved_ids[:k]
    hits = sum(1 for doc_id in retrieved_ids if doc_id in relevant_ids)
    return hits / len(relevant_ids)


def precision_at_k(retrieved_ids: list[str], relevant_ids: set[str], k: int | None = None) -> float:
    """Fraction of the top-k results that are relevant."""
    if k is not None:
        retrieved_ids = retrieved_ids[:k]
    if not retrieved_ids:
        return 0.0
    hits = sum(1 for doc_id in retrieved_ids if doc_id in relevant_ids)
    return hits / len(retrieved_ids)


def ndcg_at_k(retrieved_ids: list[str], relevant_ids: set[str], k: int | None = None) -> float:
    """Normalized discounted cumulative gain at k (binary relevance)."""
    if k is not None:
        retrieved_ids = retrieved_ids[:k]
    if not retrieved_ids:
        return 0.0

    def dcg(ids: list[str]) -> float:
        return sum(
            1.0 / (idx + 1) for idx, doc_id in enumerate(ids) if doc_id in relevant_ids
        )

    actual = dcg(retrieved_ids)
    ideal_rankings = sorted(relevant_ids, key=str)
    ideal = dcg(ideal_rankings[: len(retrieved_ids)])
    return actual / ideal if ideal > 0 else 0.0


def mean_reciprocal_rank(retrieved_lists: list[list[str]], relevant_ids: set[str]) -> float:
    """Mean of reciprocal first-hit ranks across queries."""
    if not retrieved_lists:
        return 0.0
    total = 0.0
    for ids in retrieved_lists:
        for rank, doc_id in enumerate(ids, start=1):
            if doc_id in relevant_ids:
                total += 1.0 / rank
                break
    return total / len(retrieved_lists)
