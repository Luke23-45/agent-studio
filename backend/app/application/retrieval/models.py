from dataclasses import dataclass, field
from typing import Any

from backend.app.modules.rag import VectorSearchResult


@dataclass
class RetrievalResult:
    query: str
    results: list[VectorSearchResult]
    context: str
    total_results: int
    filtered_count: int
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class RetrievalConfig:
    top_k: int = 5
    score_threshold: float = 0.3
    apply_spotlighting: bool = True
    filter_by_tenant: bool = True
    max_context_length: int = 4096
