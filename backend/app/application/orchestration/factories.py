"""Shared construction helpers for the orchestration pipeline.

Used by both the conversation API route (request path) and the eval
replay worker so replayed runs are built exactly like live runs.
"""

import structlog
from typing import Any

from backend.app.domain.policy import PolicySet
from backend.app.domain.tenant import TenantConfig
from backend.app.infrastructure.db import PolicyRepository
from backend.app.modules.tenant_config import policy_set_from_db

logger = structlog.get_logger(__name__)


async def load_or_create_policy_set(db, tenant_config: TenantConfig) -> PolicySet:
    """Load the tenant's published policy set; persist an empty one on first use."""
    policy_repo = PolicyRepository(db)
    row = await policy_repo.get_by_tenant(str(tenant_config.id))
    if row:
        return policy_set_from_db(row)
    await policy_repo.create_set(str(tenant_config.id), "default", rules=[])
    return PolicySet(tenant_id=tenant_config.id, name="default", rules=[])


def get_retrieval_service(tenant_id):
    """Build the retrieval service for a tenant (vector store from settings)."""
    from backend.app.adapters.vectorstore import create_vector_store_from_settings
    from backend.app.application.retrieval import create_retrieval_service
    from backend.app.modules.rag import create_rag_service

    vector_store = create_vector_store_from_settings()
    rag_service = create_rag_service(vector_store, tenant_id=tenant_id)
    return create_retrieval_service(rag_service)


def resolve_llm_api_key(tenant_config: TenantConfig) -> str:
    """Resolve the LLM API key for the tenant's configured provider."""
    from backend.app.settings.env import settings

    provider = tenant_config.default_provider
    if provider in ("openai", "azure", "google"):
        key = getattr(settings, f"{provider.upper()}_API_KEY", None)
    elif provider == "custom":
        key = settings.CUSTOM_LLM_API_KEY
    elif provider in ("anthropic",):
        key = settings.ANTHROPIC_API_KEY
    else:
        key = None
    if not key:
        logger.warning("no_llm_api_key_configured", provider=provider)
    return key or ""
