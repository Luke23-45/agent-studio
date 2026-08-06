"""Real embedding and vector indexing for ingestion jobs.

The SentenceTransformer model is loaded lazily (it is a heavy optional
dependency). When the model package or the configured model is
unavailable, a clear error is raised so queue retries / dead-lettering
surface the misconfiguration instead of silently faking embeddings.
"""

import time
from typing import Any, Awaitable, Callable, Optional

import structlog

from backend.app.adapters.vectorstore.provider import BaseVectorStore, VectorDocument
from backend.app.settings.env import get_settings

logger = structlog.get_logger(__name__)

EmbedderFn = Callable[[list[str]], Awaitable[list[list[float]]]]


def _run_sync(fn: Callable[[], Any]) -> Any:
    import asyncio

    loop = asyncio.get_event_loop()
    return loop.run_in_executor(None, fn)


class SentenceTransformerEmbedder:
    """Lazy SentenceTransformer-backed embedder (real, CPU-bound)."""

    def __init__(self, model_name: str | None = None):
        self.model_name = model_name or get_settings().EMBEDDING_MODEL
        self._model = None

    def _load(self):
        if self._model is not None:
            return self._model
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as e:
            raise RuntimeError(
                "sentence-transformers is not installed; "
                "install it or set EMBEDDING_MODEL to an available model "
                "to enable real embedding generation"
            ) from e
        self._model = SentenceTransformer(self.model_name)
        return self._model

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        model = await _run_sync(self._load)
        vectors = await _run_sync(lambda: model.encode(texts, normalize_embeddings=True))
        return [list(map(float, v)) for v in vectors]


async def embed_and_index(
    chunks: list[dict[str, Any]],
    vector_store: BaseVectorStore,
    tenant_id: str,
    document_id: str,
    source: str,
    embedder: EmbedderFn | None = None,
    embedding_dim: int = 384,
) -> list[str]:
    """Embed chunks and upsert them into the vector store.

    Each vector carries ``stored_at`` (unix ts) plus tenant/document/source
    metadata so cleanup can apply retention policies. Returns stored IDs.
    """
    if not chunks:
        return []
    if embedder is None:
        embedder = SentenceTransformerEmbedder().embed

    texts = [chunk["content"] for chunk in chunks]
    vectors = await embedder(texts)

    stored_at = time.time()
    documents = []
    for chunk, vector in zip(chunks, vectors):
        if len(vector) != embedding_dim:
            raise RuntimeError(
                f"Embedder returned dimension {len(vector)}, expected {embedding_dim}; "
                f"set VECTOR_STORE and EMBEDDING_MODEL consistently"
            )
        documents.append(
            VectorDocument(
                id=f"{document_id}:{chunk['id']}",
                content=chunk["content"],
                embedding=vector,
                metadata={
                    "tenant_id": str(tenant_id),
                    "document_id": document_id,
                    "source": source,
                    "chunk_id": chunk["id"],
                    "stored_at": stored_at,
                },
            )
        )

    ids = await vector_store.add_documents(documents)
    logger.info(
        "vectors_indexed",
        document_id=document_id,
        chunks=len(documents),
        store=str(vector_store.store_type.value),
    )
    return ids
