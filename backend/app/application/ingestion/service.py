import hashlib
import re
import structlog
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Set
from uuid import UUID

from ...infrastructure.patterns import ManagedService, HealthComponent, HealthStatus
from .models import DocumentStatus, IngestionJob, ChunkingStrategy, ChunkingConfig

logger = structlog.get_logger(__name__)


class IngestionError(Exception):
    def __init__(self, message: str, source: str = ""):
        self.source = source
        super().__init__(f"Ingestion error [{source}]: {message}")


class UnsupportedFormatError(IngestionError):
    def __init__(self, format: str):
        self.format = format
        super().__init__(f"Unsupported document format: {format}", source=format)


class DocumentTooLargeError(IngestionError):
    def __init__(self, size: int, max_size: int):
        super().__init__(f"Document size {size} exceeds maximum {max_size}", source=str(size))


class DuplicateDocumentError(IngestionError):
    def __init__(self, document_id: str):
        super().__init__(f"Duplicate document: {document_id}", source=document_id)


@dataclass
class IngestionConfig:
    chunking_strategy: ChunkingStrategy = ChunkingStrategy.RECURSIVE
    chunk_size: int = 512
    chunk_overlap: int = 50
    separators: List[str] = field(default_factory=lambda: ["\n\n", "\n", ". ", " ", ""])
    max_document_size: int = 10 * 1024 * 1024
    supported_formats: Set[str] = field(default_factory=lambda: {"txt", "md", "pdf", "docx", "html", "json", "csv"})
    max_concurrent_jobs: int = 10
    deduplication_enabled: bool = True
    progress_callback: Optional[Callable[[str, int, int], None]] = None


SUPPORTED_FORMATS = {"txt", "md", "pdf", "docx", "html", "json", "csv"}


class IngestionService(ManagedService):
    def __init__(self, config: Optional[IngestionConfig] = None):
        super().__init__("ingestion_service")
        self.config = config or IngestionConfig()
        self._jobs: Dict[str, IngestionJob] = {}
        self._content_hashes: Set[str] = set()
        self._active_jobs: int = 0

    async def _do_initialize(self) -> None:
        logger.info("ingestion_service_initialized",
                    strategy=self.config.chunking_strategy.value,
                    chunk_size=self.config.chunk_size)

    async def _do_close(self) -> None:
        self._jobs.clear()
        self._content_hashes.clear()

    def _detect_format(self, source: str, content: Optional[str] = None) -> str:
        ext = source.rsplit(".", 1)[-1].lower() if "." in source else ""
        if ext in self.config.supported_formats:
            return ext
        if content and content.strip().startswith("{"):
            return "json"
        if content and content.strip().startswith("<"):
            return "html"
        return ext or "txt"

    def _compute_content_hash(self, content: str, source: str) -> str:
        return hashlib.sha256(f"{source}:{content[:5000]}".encode()).hexdigest()

    async def ingest_document(
        self,
        content: str,
        source: str,
        tenant_id: Optional[UUID] = None,
        document_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> IngestionJob:
        fmt = self._detect_format(source, content)
        if fmt not in self.config.supported_formats:
            raise UnsupportedFormatError(fmt)

        content_bytes = len(content.encode("utf-8"))
        if content_bytes > self.config.max_document_size:
            raise DocumentTooLargeError(content_bytes, self.config.max_document_size)

        if self.config.deduplication_enabled:
            content_hash = self._compute_content_hash(content, source)
            if content_hash in self._content_hashes:
                raise DuplicateDocumentError(content_hash[:16])

        if self._active_jobs >= self.config.max_concurrent_jobs:
            raise IngestionError("Max concurrent jobs reached", source=source)

        job = IngestionJob(
            tenant_id=tenant_id,
            document_id=document_id or self._generate_document_id(content, source),
            source=source,
            content=content,
            metadata=metadata or {},
            status=DocumentStatus.PROCESSING,
        )
        self._jobs[job.id] = job
        self._active_jobs += 1

        try:
            if self.config.deduplication_enabled:
                self._content_hashes.add(content_hash)

            chunks = await self._chunk_document_async(content)
            job.chunks = chunks
            job.status = DocumentStatus.INDEXED
            job.completed_at = datetime.utcnow()

            if self.config.progress_callback:
                self.config.progress_callback(job.id, len(chunks), len(chunks))

            logger.info("document_ingested",
                        job_id=job.id, document_id=job.document_id,
                        chunk_count=len(chunks), format=fmt, source=source)
        except Exception as e:
            logger.error("ingestion_failed", job_id=job.id, error=str(e))
            job.status = DocumentStatus.FAILED
            job.error = str(e)
            job.completed_at = datetime.utcnow()
            raise IngestionError(str(e), source=source) from e
        finally:
            self._active_jobs -= 1

        return job

    async def _chunk_document_async(self, content: str) -> List[Dict[str, Any]]:
        loop = __import__("asyncio").get_event_loop()
        return await loop.run_in_executor(None, self._chunk_document, content)

    def _chunk_document(self, content: str) -> List[Dict[str, Any]]:
        if not content:
            return []

        strategy = self.config.chunking_strategy
        chunk_size = self.config.chunk_size
        chunk_overlap = self.config.chunk_overlap

        if strategy == ChunkingStrategy.FIXED_SIZE:
            return self._chunk_fixed_size(content, chunk_size, chunk_overlap)
        elif strategy == ChunkingStrategy.SENTENCE:
            return self._chunk_by_sentence(content, chunk_size)
        elif strategy == ChunkingStrategy.PARAGRAPH:
            return self._chunk_by_paragraph(content, chunk_size)
        return self._chunk_recursive(content, chunk_size, chunk_overlap, self.config.separators)

    def _chunk_fixed_size(self, content: str, chunk_size: int, overlap: int) -> List[Dict[str, Any]]:
        chunks = []
        start = 0
        chunk_id = 0
        while start < len(content):
            end = min(start + chunk_size, len(content))
            chunks.append({
                "id": f"chunk-{chunk_id}",
                "content": content[start:end],
                "start_offset": start,
                "end_offset": end,
            })
            start += chunk_size - overlap
            chunk_id += 1
        return chunks

    def _chunk_by_sentence(self, content: str, chunk_size: int) -> List[Dict[str, Any]]:
        sentences = re.split(r"(?<=[.!?])\s+", content)
        chunks = []
        current = ""
        chunk_id = 0
        offset = 0
        for s in sentences:
            if len(current) + len(s) <= chunk_size:
                current += (" " + s) if current else s
            else:
                if current:
                    chunks.append({"id": f"chunk-{chunk_id}", "content": current.strip(), "start_offset": offset, "end_offset": offset + len(current)})
                    chunk_id += 1
                offset += len(current)
                current = s
        if current:
            chunks.append({"id": f"chunk-{chunk_id}", "content": current.strip(), "start_offset": offset, "end_offset": offset + len(current)})
        return chunks

    def _chunk_by_paragraph(self, content: str, chunk_size: int) -> List[Dict[str, Any]]:
        paragraphs = [p.strip() for p in content.split("\n\n") if p.strip()]
        chunks = []
        current = ""
        chunk_id = 0
        offset = 0
        for p in paragraphs:
            if len(current) + len(p) <= chunk_size:
                current += "\n\n" + p if current else p
            else:
                if current:
                    chunks.append({"id": f"chunk-{chunk_id}", "content": current, "start_offset": offset, "end_offset": offset + len(current)})
                    chunk_id += 1
                offset += len(current)
                current = p
        if current:
            chunks.append({"id": f"chunk-{chunk_id}", "content": current, "start_offset": offset, "end_offset": offset + len(current)})
        return chunks

    def _chunk_recursive(self, content: str, chunk_size: int, overlap: int, separators: List[str]) -> List[Dict[str, Any]]:
        splits = [content]
        for sep in separators:
            new_splits = []
            for s in splits:
                if len(s) <= chunk_size:
                    new_splits.append(s)
                else:
                    parts = s.split(sep)
                    current = ""
                    for part in parts:
                        if len(current) + len(part) <= chunk_size:
                            current += sep + part if current else part
                        else:
                            if current:
                                new_splits.append(current)
                            current = part
                    if current:
                        new_splits.append(current)
            splits = new_splits

        chunks = []
        chunk_id = 0
        start = 0
        for i, s in enumerate(splits):
            if i > 0 and overlap > 0:
                prev = chunks[-1]["content"]
                s = (prev[-overlap:] if len(prev) > overlap else prev) + s
            chunks.append({"id": f"chunk-{chunk_id}", "content": s, "start_offset": start, "end_offset": start + len(s)})
            start += len(s) - (overlap if i > 0 else 0)
            chunk_id += 1
        return chunks

    def _generate_document_id(self, content: str, source: str) -> str:
        return hashlib.sha256(f"{source}:{content[:1000]}".encode()).hexdigest()[:32]

    def get_job_status(self, job_id: str) -> Optional[IngestionJob]:
        return self._jobs.get(job_id)

    def list_jobs(self, tenant_id: Optional[UUID] = None) -> List[IngestionJob]:
        jobs = list(self._jobs.values())
        if tenant_id:
            jobs = [j for j in jobs if j.tenant_id == tenant_id]
        return sorted(jobs, key=lambda j: j.created_at or datetime.min, reverse=True)

    def get_job_statistics(self) -> Dict[str, Any]:
        total = len(self._jobs)
        indexed = sum(1 for j in self._jobs.values() if j.status == DocumentStatus.INDEXED)
        failed = sum(1 for j in self._jobs.values() if j.status == DocumentStatus.FAILED)
        processing = sum(1 for j in self._jobs.values() if j.status == DocumentStatus.PROCESSING)
        total_chunks = sum(len(j.chunks) for j in self._jobs.values())
        return {
            "total_jobs": total,
            "indexed": indexed,
            "failed": failed,
            "processing": processing,
            "total_chunks": total_chunks,
            "active_jobs": self._active_jobs,
            "deduplication_enabled": self.config.deduplication_enabled,
            "supported_formats": list(SUPPORTED_FORMATS),
        }

    async def _do_health_check(self) -> HealthComponent:
        return HealthComponent(
            name=self.name,
            status=HealthStatus.HEALTHY,
            metadata=self.get_job_statistics(),
        )


def create_ingestion_service(
    chunking_strategy: ChunkingStrategy = ChunkingStrategy.RECURSIVE,
    chunk_size: int = 512,
    chunk_overlap: int = 50,
    max_document_size: int = 10 * 1024 * 1024,
) -> IngestionService:
    config = IngestionConfig(
        chunking_strategy=chunking_strategy,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        max_document_size=max_document_size,
    )
    return IngestionService(config)
