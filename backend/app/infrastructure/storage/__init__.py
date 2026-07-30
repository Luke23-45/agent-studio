"""
Storage infrastructure layer.

Provides S3-compatible object storage for documents, exports, and artifacts.
"""

import io
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class StorageManager:
    """Manages S3-compatible object storage."""

    def __init__(
        self,
        endpoint_url: str | None = None,
        aws_access_key_id: str | None = None,
        aws_secret_access_key: str | None = None,
        region_name: str = "us-east-1",
        bucket_name: str = "neryva-storage",
    ):
        self.endpoint_url = endpoint_url
        self.aws_access_key_id = aws_access_key_id
        self.aws_secret_access_key = aws_secret_access_key
        self.region_name = region_name
        self.bucket_name = bucket_name
        self._client: Any | None = None

    async def initialize(self) -> None:
        """Initialize S3 client."""
        try:
            import aioboto3

            session = aioboto3.Session()
            self._client = session.client(
                "s3",
                endpoint_url=self.endpoint_url,
                aws_access_key_id=self.aws_access_key_id,
                aws_secret_access_key=self.aws_secret_access_key,
                region_name=self.region_name,
            )
            logger.info(
                "storage_initialized",
                endpoint=self.endpoint_url or "aws-s3",
                bucket=self.bucket_name,
            )
        except ImportError:
            logger.warning("aioboto3_not_available", message="aioboto3 not installed")
        except Exception as e:
            logger.error("storage_init_error", error=str(e))

    async def close(self) -> None:
        """Close S3 client."""
        self._client = None
        logger.info("storage_closed")

    async def upload_file(
        self,
        key: str,
        content: bytes,
        content_type: str = "application/octet-stream",
    ) -> bool:
        """Upload a file to storage."""
        if self._client is None:
            return False

        try:
            async with self._client:
                await self._client.put_object(
                    Bucket=self.bucket_name,
                    Key=key,
                    Body=content,
                    ContentType=content_type,
                )
            logger.info("file_uploaded", key=key, size=len(content))
            return True
        except Exception as e:
            logger.error("upload_error", key=key, error=str(e))
            return False

    async def download_file(self, key: str) -> bytes | None:
        """Download a file from storage."""
        if self._client is None:
            return None

        try:
            async with self._client:
                response = await self._client.get_object(
                    Bucket=self.bucket_name,
                    Key=key,
                )
                content = await response["Body"].read()
            logger.info("file_downloaded", key=key, size=len(content))
            return content
        except Exception as e:
            logger.error("download_error", key=key, error=str(e))
            return None

    async def delete_file(self, key: str) -> bool:
        """Delete a file from storage."""
        if self._client is None:
            return False

        try:
            async with self._client:
                await self._client.delete_object(
                    Bucket=self.bucket_name,
                    Key=key,
                )
            logger.info("file_deleted", key=key)
            return True
        except Exception as e:
            logger.error("delete_error", key=key, error=str(e))
            return False

    async def list_files(self, prefix: str = "") -> list[str]:
        """List files in storage with optional prefix."""
        if self._client is None:
            return []

        try:
            keys = []
            async with self._client:
                paginator = self._client.get_paginator("list_objects_v2")
                async for page in paginator.paginate(
                    Bucket=self.bucket_name,
                    Prefix=prefix,
                ):
                    for obj in page.get("Contents", []):
                        keys.append(obj["Key"])
            logger.info("files_listed", prefix=prefix, count=len(keys))
            return keys
        except Exception as e:
            logger.error("list_error", prefix=prefix, error=str(e))
            return []

    async def health_check(self) -> bool:
        """Check storage connectivity."""
        if self._client is None:
            return False

        try:
            async with self._client:
                await self._client.head_bucket(Bucket=self.bucket_name)
            return True
        except Exception as e:
            logger.error("storage_health_check_failed", error=str(e))
            return False


# Global storage instance
_storage_manager: StorageManager | None = None


def get_storage_manager() -> StorageManager:
    """Get the global storage manager instance."""
    if _storage_manager is None:
        raise RuntimeError("Storage manager not initialized")
    return _storage_manager


def init_storage(
    endpoint_url: str | None = None,
    aws_access_key_id: str | None = None,
    aws_secret_access_key: str | None = None,
    region_name: str = "us-east-1",
    bucket_name: str = "neryva-storage",
    **kwargs,
) -> StorageManager:
    """Initialize the global storage manager."""
    global _storage_manager
    _storage_manager = StorageManager(
        endpoint_url=endpoint_url,
        aws_access_key_id=aws_access_key_id,
        aws_secret_access_key=aws_secret_access_key,
        region_name=region_name,
        bucket_name=bucket_name,
        **kwargs,
    )
    return _storage_manager