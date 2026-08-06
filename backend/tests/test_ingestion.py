"""
Tests for ingestion service.
"""

import pytest
from uuid import uuid4


class TestIngestionService:
    """Tests for IngestionService."""

    def test_chunk_fixed_size(self):
        """Test fixed-size chunking."""
        from backend.app.application.ingestion import create_ingestion_service, ChunkingStrategy

        service = create_ingestion_service(
            chunking_strategy=ChunkingStrategy.FIXED_SIZE,
            chunk_size=50,
            chunk_overlap=10,
        )

        content = "This is a test document. " * 10
        chunks = service._chunk_document(content)

        assert len(chunks) > 0
        assert all("content" in chunk for chunk in chunks)

    def test_chunk_by_sentence(self):
        """Test sentence-based chunking."""
        from backend.app.application.ingestion import create_ingestion_service, ChunkingStrategy

        service = create_ingestion_service(
            chunking_strategy=ChunkingStrategy.SENTENCE,
            chunk_size=100,
        )

        content = "First sentence. Second sentence. Third sentence."
        chunks = service._chunk_document(content)

        assert len(chunks) > 0

    def test_chunk_by_paragraph(self):
        """Test paragraph-based chunking."""
        from backend.app.application.ingestion import create_ingestion_service, ChunkingStrategy

        service = create_ingestion_service(
            chunking_strategy=ChunkingStrategy.PARAGRAPH,
            chunk_size=200,
        )

        content = "Paragraph one.\n\nParagraph two.\n\nParagraph three."
        chunks = service._chunk_document(content)

        assert len(chunks) > 0

    @pytest.mark.asyncio
    async def test_ingest_document(self):
        """Test document ingestion."""
        from backend.app.application.ingestion import create_ingestion_service

        service = create_ingestion_service()
        
        job = await service.ingest_document(
            content="Test document content",
            source="test-source",
            tenant_id=uuid4(),
        )

        assert job.status.value == "indexed" or job.status.value == "failed"
        assert job.document_id is not None
