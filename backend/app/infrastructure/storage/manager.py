import os
import time
import structlog
import hashlib
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from ..patterns import ManagedService, HealthComponent, HealthStatus
from ..patterns.retry import AsyncRetry, RetryConfig, ExponentialBackoff
from ..patterns.circuit_breaker import CircuitBreaker, CircuitBreakerConfig

logger = structlog.get_logger(__name__)


class FileNotFound(Exception):
    def __init__(self, key: str):
        self.key = key
        super().__init__(f"File not found in storage: {key}")


class ChecksumMismatch(Exception):
    def __init__(self, key: str, expected: str, actual: str):
        self.key = key
        super().__init__(f"Checksum mismatch for {key}: expected {expected}, got {actual}")


@dataclass
class StorageConfig:
    endpoint_url: Optional[str] = None
    region_name: str = "us-east-1"
    bucket_name: str = "neryva-storage"
    aws_access_key_id: Optional[str] = None
    aws_secret_access_key: Optional[str] = None
    max_retries: int = 3
    retry_min_delay: float = 1.0
    multipart_threshold_mb: int = 100
    multipart_chunk_size_mb: int = 10
    presign_url_expiry: int = 3600
    circuit_breaker_failures: int = 3
    circuit_breaker_recovery: float = 15.0


@dataclass
class FileMetadata:
    key: str
    size: int
    content_type: str
    checksum_sha256: str
    uploaded_at: str
    metadata: Dict[str, str] = field(default_factory=dict)


class StorageManager(ManagedService):
    def __init__(self, config: Optional[StorageConfig] = None):
        super().__init__("storage")
        self.config = config or StorageConfig()
        self._client: Any = None
        self._retry = AsyncRetry(RetryConfig(
            max_attempts=self.config.max_retries,
            backoff=ExponentialBackoff(min_delay=self.config.retry_min_delay),
        ))
        self._circuit_breaker = CircuitBreaker(CircuitBreakerConfig(
            name="storage",
            failure_threshold=self.config.circuit_breaker_failures,
            recovery_timeout=self.config.circuit_breaker_recovery,
        ))

    async def _do_initialize(self) -> None:
        try:
            import aioboto3
            session = aioboto3.Session(
                aws_access_key_id=self.config.aws_access_key_id,
                aws_secret_access_key=self.config.aws_secret_access_key,
                region_name=self.config.region_name,
            )
            self._session = session
            self._client = session.client("s3", endpoint_url=self.config.endpoint_url)
            await self._ensure_bucket_exists()
            logger.info("storage_initialized", bucket=self.config.bucket_name, endpoint=self.config.endpoint_url or "aws")
        except Exception as e:
            # aioboto3 missing, S3 unreachable, or bucket creation failed:
            # degrade to the local filesystem instead of failing startup.
            logger.warning(
                "storage_unavailable_falling_back_to_local_fs",
                bucket=self.config.bucket_name,
                error=str(e),
            )
            self._client = None
            self._local_path = f"./data/storage/{self.config.bucket_name}"
            os.makedirs(self._local_path, exist_ok=True)

    async def _ensure_bucket_exists(self) -> None:
        if not self._client:
            return
        try:
            await self._client.head_bucket(Bucket=self.config.bucket_name)
        except Exception:
            await self._client.create_bucket(Bucket=self.config.bucket_name)
            logger.info("bucket_created", bucket=self.config.bucket_name)

    async def _do_close(self) -> None:
        self._client = None

    async def upload_file(self, key: str, content: bytes, content_type: str = "application/octet-stream", metadata: Optional[Dict[str, str]] = None) -> FileMetadata:
        checksum = hashlib.sha256(content).hexdigest()
        size = len(content)

        if self._client:
            async def _do_upload() -> None:
                async with self._client as s3:
                    extra = {"ContentType": content_type}
                    if metadata:
                        extra["Metadata"] = metadata
                    await s3.put_object(Bucket=self.config.bucket_name, Key=key, Body=content, **extra)

            await self._retry.execute(self._circuit_breaker.call, _do_upload)
        else:
            local_key = key.replace("/", os.sep)
            full_path = os.path.join(self._local_path, local_key)
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "wb") as f:
                f.write(content)

        from datetime import datetime
        result = FileMetadata(
            key=key, size=size, content_type=content_type,
            checksum_sha256=checksum, uploaded_at=datetime.utcnow().isoformat(),
            metadata=metadata or {},
        )
        logger.info("file_uploaded", key=key, size=size, checksum=checksum[:16])
        return result

    async def download_file(self, key: str) -> Tuple[bytes, FileMetadata]:
        if self._client:
            try:
                async def _do_download() -> Tuple[bytes, Dict]:
                    async with self._client as s3:
                        response = await s3.get_object(Bucket=self.config.bucket_name, Key=key)
                        content = await response["Body"].read()
                        return content, response.get("Metadata", {})

                content, meta = await self._retry.execute(self._circuit_breaker.call, _do_download)
            except Exception as e:
                raise FileNotFound(key) from e
        else:
            local_key = key.replace("/", os.sep)
            full_path = os.path.join(self._local_path, local_key)
            if not os.path.exists(full_path):
                raise FileNotFound(key)
            with open(full_path, "rb") as f:
                content = f.read()
            meta = {}

        checksum = hashlib.sha256(content).hexdigest()
        logger.info("file_downloaded", key=key, size=len(content))
        return content, FileMetadata(
            key=key, size=len(content), content_type="",
            checksum_sha256=checksum, uploaded_at="",
            metadata=meta,
        )

    async def download_file_verified(self, key: str, expected_checksum: str) -> Tuple[bytes, FileMetadata]:
        content, meta = await self.download_file(key)
        actual = hashlib.sha256(content).hexdigest()
        if actual != expected_checksum:
            raise ChecksumMismatch(key, expected_checksum, actual)
        return content, meta

    async def delete_file(self, key: str) -> bool:
        if self._client:
            try:
                async def _do_delete() -> None:
                    async with self._client as s3:
                        await s3.delete_object(Bucket=self.config.bucket_name, Key=key)
                await self._retry.execute(_do_delete)
                logger.info("file_deleted", key=key)
                return True
            except Exception as e:
                logger.error("delete_error", key=key, error=str(e))
                return False
        else:
            local_key = key.replace("/", os.sep)
            full_path = os.path.join(self._local_path, local_key)
            if os.path.exists(full_path):
                os.remove(full_path)
                return True
            return False

    async def list_files(self, prefix: str = "") -> List[FileMetadata]:
        if self._client:
            try:
                async def _do_list() -> List[FileMetadata]:
                    keys: List[FileMetadata] = []
                    async with self._client as s3:
                        paginator = s3.get_paginator("list_objects_v2")
                        async for page in paginator.paginate(Bucket=self.config.bucket_name, Prefix=prefix):
                            for obj in page.get("Contents", []):
                                keys.append(FileMetadata(
                                    key=obj["Key"], size=obj["Size"],
                                    content_type="", checksum_sha256=obj.get("ETag", "").strip('"'),
                                    uploaded_at=obj.get("LastModified", "").isoformat() if hasattr(obj.get("LastModified"), "isoformat") else str(obj.get("LastModified", "")),
                                ))
                    return keys

                return await self._retry.execute(_do_list)
            except Exception as e:
                logger.error("list_error", prefix=prefix, error=str(e))
                return []
        else:
            results = []
            full_prefix = os.path.join(self._local_path, prefix.replace("/", os.sep))
            if os.path.exists(full_prefix):
                for root, dirs, files in os.walk(full_prefix):
                    for f in files:
                        fp = os.path.join(root, f)
                        rel = os.path.relpath(fp, self._local_path)
                        stats = os.stat(fp)
                        results.append(FileMetadata(
                            key=rel.replace(os.sep, "/"), size=stats.st_size,
                            content_type="", checksum_sha256="", uploaded_at="",
                        ))
            return results

    async def file_exists(self, key: str) -> bool:
        if self._client:
            try:
                async def _do_check() -> bool:
                    async with self._client as s3:
                        await s3.head_object(Bucket=self.config.bucket_name, Key=key)
                    return True
                return await _do_check()
            except Exception:
                return False
        else:
            local_key = key.replace("/", os.sep)
            return os.path.exists(os.path.join(self._local_path, local_key))

    async def copy_file(self, source_key: str, dest_key: str) -> bool:
        if self._client:
            try:
                async def _do_copy() -> None:
                    async with self._client as s3:
                        copy_source = {"Bucket": self.config.bucket_name, "Key": source_key}
                        await s3.copy_object(CopySource=copy_source, Bucket=self.config.bucket_name, Key=dest_key)
                await self._retry.execute(_do_copy)
                return True
            except Exception as e:
                logger.error("copy_error", source=source_key, dest=dest_key, error=str(e))
                return False
        return False

    async def generate_presigned_url(self, key: str, expires_in: Optional[int] = None) -> Optional[str]:
        if not self._client:
            return None
        try:
            expires = expires_in or self.config.presign_url_expiry
            url = await self._client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.config.bucket_name, "Key": key},
                ExpiresIn=expires,
            )
            return url
        except Exception as e:
            logger.error("presign_error", key=key, error=str(e))
            return None

    async def _do_health_check(self) -> HealthComponent:
        if self._client:
            try:
                start = time.time()
                async with self._client as s3:
                    await s3.head_bucket(Bucket=self.config.bucket_name)
                latency = time.time() - start
                return HealthComponent(
                    name=self.name,
                    status=HealthStatus.HEALTHY if latency < 1.0 else HealthStatus.DEGRADED,
                    metadata={"bucket": self.config.bucket_name, "latency_ms": latency * 1000, "type": "s3"},
                )
            except Exception as e:
                return HealthComponent(name=self.name, status=HealthStatus.DEGRADED, message=str(e))
        return HealthComponent(name=self.name, status=HealthStatus.HEALTHY, message="Local filesystem storage", metadata={"type": "local"})


_storage_manager: Optional[StorageManager] = None


def get_storage_manager() -> StorageManager:
    if _storage_manager is None:
        raise RuntimeError("Storage manager not initialized")
    return _storage_manager


def init_storage(
    endpoint_url: Optional[str] = None,
    bucket_name: str = "neryva-storage",
    **kwargs,
) -> StorageManager:
    global _storage_manager
    config = StorageConfig(endpoint_url=endpoint_url, bucket_name=bucket_name, **kwargs)
    _storage_manager = StorageManager(config)
    return _storage_manager
