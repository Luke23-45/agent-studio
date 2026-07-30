"""
Ingestion service for document processing.

Handles document upload, parsing, chunking, and indexing into the knowledge base.
"""

import hashlib
import structlog
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

logger = structlog.get_logger(__name__)


class DocumentStatus(str, Enum):
    """Status of a document in the ingestion pipeline."""

    PENDING = "pending"
    PROCESSING = "processing"
    INDEXED = "indexed"
    FAILED = "failed"


@dataclass
class IngestionJob:
    """Represents a document ingestion job."""

    id: str = field(default_factory=lambda: str(uuid4()))
    tenant_id: UUID | None = None
    document_id: str | None = None
    source: str = ""
    content: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    status: DocumentStatus = DocumentStatus.PENDING
    chunks: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    completed_at: datetime | None = None


class ChunkingStrategy(str, Enum):
    """Available chunking strategies."""

    FIXED_SIZE = "fixed_size"
    SENTENCE = "sentence"
    PARAGRAPH = "paragraph"
    RECURSIVE = "recursive"


@dataclass
class ChunkingConfig:
    """Configuration for document chunking."""

    strategy: ChunkingStrategy = ChunkingStrategy.RECURSIVE
    chunk_size: int = 512
    chunk_overlap: int = 50
    separators: list[str] = field(default_factory=lambda: ["\n\n", "\n", ". ", " ", ""])


class IngestionService:
    """Service for ingesting documents into the knowledge base."""

    def __init__(
        self,
        chunking_config: ChunkingConfig | None = None,
        max_document_size: int = 10 * 1024 * 1024,  # 10MB
    ):
        self.chunking_config = chunking_config or ChunkingConfig()
        self.max_document_size = max_document_size
        self._job_cache: dict[str, IngestionJob] = {}

    async def ingest_document(
        self,
        content: str,
        source: str,
        tenant_id: UUID | None = None,
        document_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> IngestionJob:
        """Ingest a document into the knowledge base."""
        logger.info(
            "ingesting_document",
            source=source,
            content_length=len(content),
            tenant_id=tenant_id,
        )

        # Validate document size
        if len(content.encode("utf-8")) > self.max_document_size:
            error_msg = f"Document exceeds maximum size of {self.max_document_size} bytes"
            logger.error("document_too_large", error=error_msg)
            return IngestionJob(
                status=DocumentStatus.FAILED,
                error=error_msg,
                source=source,
                content=content,
            )

        # Create ingestion job
        job = IngestionJob(
            tenant_id=tenant_id,
            document_id=document_id or self._generate_document_id(content, source),
            source=source,
            content=content,
            metadata=metadata or {},
            status=DocumentStatus.PROCESSING,
        )

        self._job_cache[job.id] = job

        try:
            # Chunk the document
            chunks = self._chunk_document(content)
            job.chunks = chunks

            # Mark as indexed (actual indexing happens in RAG service)
            job.status = DocumentStatus.INDEXED
            job.completed_at = datetime.utcnow()

            logger.info(
                "document_ingested",
                job_id=job.id,
                document_id=job.document_id,
                chunk_count=len(chunks),
            )
        except Exception as e:
            logger.error("ingestion_error", error=str(e), job_id=job.id)
            job.status = DocumentStatus.FAILED
            job.error = str(e)
            job.completed_at = datetime.utcnow()

        return job

    def _chunk_document(self, content: str) -> list[dict[str, Any]]:
        """Split document into chunks based on configured strategy."""
        strategy = self.chunking_config.strategy
        chunk_size = self.chunking_config.chunk_size
        chunk_overlap = self.chunking_config.chunk_overlap

        if strategy == ChunkingStrategy.FIXED_SIZE:
            return self._chunk_fixed_size(content, chunk_size, chunk_overlap)
        elif strategy == ChunkingStrategy.SENTENCE:
            return self._chunk_by_sentence(content, chunk_size)
        elif strategy == ChunkingStrategy.PARAGRAPH:
            return self._chunk_by_paragraph(content, chunk_size)
        else:  # RECURSIVE
            return self._chunk_recursive(
                content, chunk_size, chunk_overlap, self.chunking_config.separators
            )

    def _chunk_fixed_size(
        self, content: str, chunk_size: int, overlap: int
    ) -> list[dict[str, Any]]:
        """Split content into fixed-size chunks with overlap."""
        chunks = []
        start = 0
        chunk_id = 0

        while start < len(content):
            end = start + chunk_size
            chunk_text = content[start:end]
            chunks.append(
                {
                    "id": f"chunk-{chunk_id}",
                    "content": chunk_text,
                    "start_offset": start,
                    "end_offset": end,
                }
            )
            start += chunk_size - overlap
            chunk_id += 1

        return chunks

    def _chunk_by_sentence(self, content: str, chunk_size: int) -> list[dict[str, Any]]:
        """Split content by sentences, grouping into chunks."""
        import re

        # Simple sentence splitting
        sentences = re.split(r"(?<=[.!?])\s+", content)
        chunks = []
        current_chunk = ""
        chunk_id = 0
        start_offset = 0

        for sentence in sentences:
            if len(current_chunk) + len(sentence) <= chunk_size:
                current_chunk += " " + sentence if current_chunk else sentence
            else:
                if current_chunk:
                    chunks.append(
                        {
                            "id": f"chunk-{chunk_id}",
                            "content": current_chunk.strip(),
                            "start_offset": start_offset,
                            "end_offset": start_offset + len(current_chunk),
                        }
                    )
                    chunk_id += 1
                start_offset += len(current_chunk)
                current_chunk = sentence

        if current_chunk:
            chunks.append(
                {
                    "id": f"chunk-{chunk_id}",
                    "content": current_chunk.strip(),
                    "start_offset": start_offset,
                    "end_offset": start_offset + len(current_chunk),
                }
            )

        return chunks

    def _chunk_by_paragraph(self, content: str, chunk_size: int) -> list[dict[str, Any]]:
        """Split content by paragraphs, grouping into chunks."""
        paragraphs = content.split("\n\n")
        chunks = []
        current_chunk = ""
        chunk_id = 0
        start_offset = 0

        for paragraph in paragraphs:
            paragraph = paragraph.strip()
            if not paragraph:
                continue

            if len(current_chunk) + len(paragraph) <= chunk_size:
                current_chunk += "\n\n" + paragraph if current_chunk else paragraph
            else:
                if current_chunk:
                    chunks.append(
                        {
                            "id": f"chunk-{chunk_id}",
                            "content": current_chunk,
                            "start_offset": start_offset,
                            "end_offset": start_offset + len(current_chunk),
                        }
                    )
                    chunk_id += 1
                start_offset += len(current_chunk)
                current_chunk = paragraph

        if current_chunk:
            chunks.append(
                {
                    "id": f"chunk-{chunk_id}",
                    "content": current_chunk,
                    "start_offset": start_offset,
                    "end_offset": start_offset + len(current_chunk),
                }
            )

        return chunks

    def _chunk_recursive(
        self,
        content: str,
        chunk_size: int,
        overlap: int,
        separators: list[str],
    ) -> list[dict[str, Any]]:
        """Recursively split content using multiple separators."""
        chunks = [content]

        for separator in separators:
            new_chunks = []
            for chunk in chunks:
                if len(chunk) <= chunk_size:
                    new_chunks.append(chunk)
                else:
                    parts = chunk.split(separator)
                    # Recombine parts to respect chunk_size
                    current = ""
                    for part in parts:
                        if len(current) + len(part) <= chunk_size:
                            current += separator + part if current else part
                        else:
                            if current:
                                new_chunks.append(current)
                            current = part
                    if current:
                        new_chunks.append(current)
            chunks = new_chunks

        # Add overlap and create final chunk objects
        final_chunks = []
        chunk_id = 0
        start = 0

        for i, chunk in enumerate(chunks):
            # Calculate overlap from previous chunk
            if i > 0 and overlap > 0:
                prev_chunk = final_chunks[-1]["content"]
                overlap_text = prev_chunk[-overlap:] if len(prev_chunk) > overlap else prev_chunk
                chunk = overlap_text + chunk

            final_chunks.append(
                {
                    "id": f"chunk-{chunk_id}",
                    "content": chunk,
                    "start_offset": start,
                    "end_offset": start + len(chunk),
                }
            )
            start += len(chunk) - overlap if i > 0 else len(chunk)
            chunk_id += 1

        return final_chunks

    def _generate_document_id(self, content: str, source: str) -> str:
        """Generate a unique document ID based on content and source."""
        hash_input = f"{source}:{content[:1000]}"
        return hashlib.sha256(hash_input.encode()).hexdigest()[:32]

    def get_job_status(self, job_id: str) -> IngestionJob | None:
        """Get the status of an ingestion job."""
        return self._job_cache.get(job_id)

    def list_jobs(self, tenant_id: UUID | None = None) -> list[IngestionJob]:
        """List ingestion jobs, optionally filtered by tenant."""
        jobs = list(self._job_cache.values())
        if tenant_id:
            jobs = [j for j in jobs if j.tenant_id == tenant_id]
        return sorted(jobs, key=lambda j: j.created_at, reverse=True)


def create_ingestion_service(
    chunking_strategy: ChunkingStrategy = ChunkingStrategy.RECURSIVE,
    chunk_size: int = 512,
    chunk_overlap: int = 50,
    max_document_size: int = 10 * 1024 * 1024,
) -> IngestionService:
    """Factory function to create ingestion service."""
    config = ChunkingConfig(
        strategy=chunking_strategy,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
    )
    return IngestionService(
        chunking_config=config,
        max_document_size=max_document_size,
    )