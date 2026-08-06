"""
Per-tenant provider credential resolution (Arch 6.3.9, P0-8).

The database row is the source of truth for provider keys. The platform
fallback (settings keys) is only consulted when the tenant has no key row
and ``PLATFORM_MANAGED_KEYS_ENABLED`` is on (dev default; production is
BYOK-only). Resolution failures surface as ``ProviderKeyNotFoundError``
so callers can return a clean 503.
"""

import structlog
from typing import Any

from backend.app.infrastructure.db.repositories import TenantProviderKeyRepository
from backend.app.infrastructure.keys.crypto import decrypt_secret, encrypt_secret
from backend.app.settings.env import settings

logger = structlog.get_logger(__name__)

SUPPORTED_PROVIDERS = ("openai", "anthropic", "google", "azure", "custom")

KEY_SOURCES = ("platform-managed", "tenant-owned")


class ProviderKeyNotFoundError(Exception):
    pass


class ProviderKeyService:
    def __init__(self, db: Any):
        self._repo = TenantProviderKeyRepository(db)

    async def resolve(self, tenant_config: Any) -> str:
        """Return the decrypted API key for the tenant's default provider.

        Never logs the key. Tenant rows win; the platform-managed fallback
        applies only when the flag is on and a settings key exists.
        """
        tenant_id = str(tenant_config.id)
        provider = tenant_config.default_provider

        row = await self._repo.get(tenant_id, provider)
        if row:
            return decrypt_secret(row["encrypted_key"])

        if settings.PLATFORM_MANAGED_KEYS_ENABLED:
            key = _settings_key_for(provider)
            if key:
                logger.warning(
                    "platform_managed_key_in_use",
                    tenant_id=tenant_id,
                    provider=provider,
                    hint="set a tenant provider key to enable BYOK",
                )
                return key

        raise ProviderKeyNotFoundError(
            f"No API key configured for provider '{provider}' (tenant {tenant_id}). "
            "Set a tenant provider key or enable the platform-managed fallback."
        )

    async def set_key(
        self,
        tenant_id: str,
        provider: str,
        api_key: str,
        key_source: str = "tenant-owned",
        kms_ref: str | None = None,
    ) -> dict[str, Any]:
        """Encrypt and store/rotate a provider key; returns row info (no secret)."""
        encrypted = encrypt_secret(api_key)
        return await self._repo.upsert(
            tenant_id, provider, encrypted, key_source, kms_ref=kms_ref
        )


def _settings_key_for(provider: str) -> str | None:
    return {
        "openai": settings.OPENAI_API_KEY,
        "anthropic": settings.ANTHROPIC_API_KEY,
        "google": settings.GOOGLE_API_KEY,
        "azure": settings.AZURE_API_KEY,
        "custom": settings.CUSTOM_LLM_API_KEY,
    }.get(provider)
