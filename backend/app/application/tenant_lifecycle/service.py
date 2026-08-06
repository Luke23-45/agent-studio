"""Tenant lifecycle: GDPR export, GDPR erasure, offboarding."""

import structlog
from typing import Any

from backend.app.infrastructure.db import (
    ApiKeyRepository,
    AuditRepository,
    ConversationRepository,
    DatabaseManager,
    EscalationRepository,
    EvidenceRepository,
    PolicyRepository,
    TenantRepository,
    WebhookRepository,
    get_database_manager,
)

logger = structlog.get_logger(__name__)


class TenantLifecycleService:
    def __init__(self, db: DatabaseManager | None = None):
        self.db = db or get_database_manager()

    async def export_tenant_data(self, tenant_id: str) -> dict[str, Any]:
        """Return a portable GDPR export bundle for the tenant."""
        tenant = await TenantRepository(self.db).get_by_id(tenant_id)
        if tenant is None:
            raise ValueError(f"tenant not found: {tenant_id}")

        conversation_repo = ConversationRepository(self.db)
        conversations = await conversation_repo.list_by_tenant(tenant_id, limit=10000)
        conversation_ids = [c["id"] for c in conversations]
        messages = await conversation_repo.list_messages_all(conversation_ids)
        messages_by_conversation: dict[str, list[dict[str, Any]]] = {}
        for message in messages:
            messages_by_conversation.setdefault(message["conversation_id"], []).append(message)
        for conversation in conversations:
            conversation["messages"] = messages_by_conversation.get(conversation["id"], [])

        audit_repo = AuditRepository(self.db)
        evidence_repo = EvidenceRepository(self.db)
        escalation_repo = EscalationRepository(self.db)
        webhook_repo = WebhookRepository(self.db)
        policy_repo = PolicyRepository(self.db)

        from datetime import datetime, timezone

        return {
            "schema_version": "1.0",
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "tenant": tenant,
            "policy_set": await policy_repo.get_by_tenant(tenant_id),
            "conversations": conversations,
            "guardrail_evidence": await evidence_repo.list_by_tenant(tenant_id, limit=10000),
            "escalations": await escalation_repo.list_by_tenant(tenant_id, limit=10000),
            "webhook_events": await webhook_repo.list_events(tenant_id=tenant_id, limit=10000),
            "audit_events": await audit_repo.list_events(tenant_id=tenant_id, limit=10000),
        }

    async def erase_tenant_data(self, tenant_id: str) -> dict[str, int]:
        """GDPR erasure: delete all tenant data, keep tenant + audit trail."""
        from datetime import datetime, timezone

        conversation_repo = ConversationRepository(self.db)
        evidence_repo = EvidenceRepository(self.db)
        escalation_repo = EscalationRepository(self.db)
        webhook_repo = WebhookRepository(self.db)

        deleted = {
            "conversations_and_messages": await conversation_repo.delete_by_tenant(tenant_id),
            "guardrail_evidence": await evidence_repo.delete_by_tenant(tenant_id),
            "escalations": await escalation_repo.delete_by_tenant(tenant_id),
            "webhooks": await webhook_repo.delete_by_tenant(tenant_id),
        }
        await AuditRepository(self.db).add(
            action="tenant.data_erased",
            resource_type="tenant",
            resource_id=tenant_id,
            tenant_id=tenant_id,
            actor_type="system",
            details={
                "deleted": deleted,
                "retained": ["audit_events", "tenant"],
                "reason": "GDPR erasure request",
                "at": datetime.now(timezone.utc).isoformat(),
            },
        )
        logger.info("tenant_data_erased", tenant_id=tenant_id, **deleted)
        return deleted

    async def offboard_tenant(self, tenant_id: str) -> bool:
        """Offboard: erase tenant data, revoke its API keys, delete the tenant."""
        await self.erase_tenant_data(tenant_id)
        await ApiKeyRepository(self.db).revoke_all_for_tenant(tenant_id)
        deleted = await TenantRepository(self.db).delete(tenant_id)
        await AuditRepository(self.db).add(
            action="tenant.offboarded",
            resource_type="tenant",
            resource_id=tenant_id,
            tenant_id=tenant_id,
            actor_type="system",
        )
        logger.info("tenant_offboarded", tenant_id=tenant_id)
        return deleted
