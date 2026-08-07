"""Tenant lifecycle: GDPR export, GDPR erasure, offboarding."""

from typing import Any

import structlog

from backend.app.governance.compliance import mark_export, retention_days_for
from backend.app.governance.isolation import resolve_tenant_context
from backend.app.governance.residency import archive_prefix
from backend.app.infrastructure.db import (
    ApiKeyRepository,
    AuditRepository,
    ConversationRepository,
    DatabaseManager,
    EndUserRepository,
    EscalationRepository,
    EvidenceRepository,
    PolicyRepository,
    SessionTokenRepository,
    TenantRepository,
    WebhookRepository,
    get_database_manager,
)
from backend.app.infrastructure.db.memory import MemoryRepository
from backend.app.infrastructure.db.threads import ThreadRepository

logger = structlog.get_logger(__name__)


class TenantLifecycleService:
    def __init__(self, db: DatabaseManager | None = None):
        self.db = db or get_database_manager()

    def _retention_policy(self, tenant: dict[str, Any]) -> int:
        try:
            return tenant.get("retention_days") or retention_days_for(tenant)
        except Exception:  # pragma: no cover - defensive
            return 30

    async def export_tenant_data(self, tenant_id: str) -> dict[str, Any]:
        """Return a portable GDPR export bundle for the tenant.

        The bundle is confined to the tenant's residency archive prefix
        (P5-12) and marked as containing AI-generated content (P5-11).
        """
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

        bundle = {
            "schema_version": "1.0",
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "tenant": tenant,
            "policy_set": await policy_repo.get_by_tenant(tenant_id),
            "conversations": conversations,
            "guardrail_evidence": await evidence_repo.list_by_tenant(tenant_id, limit=10000),
            "escalations": await escalation_repo.list_by_tenant(tenant_id, limit=10000),
            "webhook_events": await webhook_repo.list_events(tenant_id=tenant_id, limit=10000),
            "audit_events": await audit_repo.list_events(tenant_id=tenant_id, limit=10000),
            "archive_prefix": archive_prefix(tenant_id, tenant.get("region")),
            "storage_prefix": resolve_tenant_context(tenant_id=tenant_id).storage_prefix(),
            "retention_days": self._retention_policy(tenant),
        }
        await audit_repo.add(
            action="tenant.data_exported",
            resource_type="tenant",
            resource_id=tenant_id,
            tenant_id=tenant_id,
            actor_type="system",
            details={
                "retention_days": self._retention_policy(tenant),
                "archive_prefix": archive_prefix(tenant_id, tenant.get("region")),
                "reason": "GDPR export request",
            },
        )
        return mark_export(bundle)

    async def erase_tenant_data(self, tenant_id: str) -> dict[str, int]:
        """GDPR erasure: delete all tenant data, keep tenant + audit trail."""
        from datetime import datetime, timezone

        conversation_repo = ConversationRepository(self.db)
        evidence_repo = EvidenceRepository(self.db)
        escalation_repo = EscalationRepository(self.db)
        webhook_repo = WebhookRepository(self.db)
        tenant = await TenantRepository(self.db).get_by_id(tenant_id)

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
                "retention_days": self._retention_policy(tenant or {}),
                "reason": "GDPR erasure request",
                "at": datetime.now(timezone.utc).isoformat(),
            },
        )
        logger.info("tenant_data_erased", tenant_id=tenant_id, **deleted)
        return deleted

    async def offboard_tenant(self, tenant_id: str) -> bool:
        """Offboard: erase data (retaining audit + governance evidence per
        retention), revoke API keys, drop isolation namespaces/prefixes, and
        delete the tenant record."""
        tenant = await TenantRepository(self.db).get_by_id(tenant_id)
        if tenant is None:
            return False

        ctx = resolve_tenant_context(tenant_id=tenant_id)
        archive = archive_prefix(tenant_id, tenant.get("region"))
        retention_days = self._retention_policy(tenant)

        conversation_repo = ConversationRepository(self.db)
        escalation_repo = EscalationRepository(self.db)
        webhook_repo = WebhookRepository(self.db)
        deleted = {
            "conversations_and_messages": await conversation_repo.delete_by_tenant(tenant_id),
            "escalations": await escalation_repo.delete_by_tenant(tenant_id),
            "webhooks": await webhook_repo.delete_by_tenant(tenant_id),
        }
        await ApiKeyRepository(self.db).revoke_all_for_tenant(tenant_id)
        deleted_tenant = await TenantRepository(self.db).delete(tenant_id)
        await AuditRepository(self.db).add(
            action="tenant.offboarded",
            resource_type="tenant",
            resource_id=tenant_id,
            tenant_id=tenant_id,
            actor_type="system",
            details={
                "deleted": deleted,
                "retained": ["audit_events", "guardrail_evidence"],
                "retention_days": retention_days,
                "dropped_namespaces": [ctx.vector_namespace()],
                "dropped_prefixes": [ctx.storage_prefix(), archive],
            },
        )
        logger.info(
            "tenant_offboarded",
            tenant_id=tenant_id,
            retention_days=retention_days,
            archive_prefix=archive,
        )
        return deleted_tenant

    async def export_end_user_data(
        self, tenant_id: str, end_user_id: str
    ) -> dict[str, Any]:
        """Return a portable DSR export bundle for one end user."""
        from datetime import datetime, timezone

        end_user = await EndUserRepository(self.db).get_by_id(
            tenant_id, end_user_id
        )
        if end_user is None:
            raise ValueError(f"end user not found: {end_user_id}")

        conversation_repo = ConversationRepository(self.db)
        thread_repo = ThreadRepository(self.db)
        memory_repo = MemoryRepository(self.db)

        threads = await thread_repo.list_threads(
            tenant_id, end_user_id=end_user_id, limit=10000
        )
        messages = await conversation_repo.list_messages_by_end_user(
            tenant_id, end_user_id, limit=10000
        )
        memories = await memory_repo.retrieve(
            tenant_id, end_user_id=end_user_id, limit=10000
        )

        bundle = {
            "schema_version": "1.0",
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "end_user": {
                "id": end_user["id"],
                "display_name": end_user.get("display_name"),
                "external_id": end_user.get("external_id"),
                "status": end_user.get("status"),
            },
            "threads": threads,
            "messages": messages,
            "memories": memories,
        }
        await AuditRepository(self.db).add(
            action="end_user.data_exported",
            resource_type="end_user",
            resource_id=end_user_id,
            tenant_id=tenant_id,
            actor_type="system",
            details={"reason": "DSR export request"},
        )
        return mark_export(bundle)

    async def erase_end_user_data(
        self, tenant_id: str, end_user_id: str
    ) -> dict[str, int]:
        """DSR erasure: delete all data and sessions for one end user."""
        from datetime import datetime, timezone

        if await EndUserRepository(self.db).get_by_id(tenant_id, end_user_id) is None:
            raise ValueError(f"end user not found: {end_user_id}")

        deleted = {
            "threads": await ThreadRepository(self.db).delete_by_end_user(
                tenant_id, end_user_id
            ),
            "messages": await ConversationRepository(self.db).delete_messages_by_end_user(
                tenant_id, end_user_id
            ),
            "memories": await MemoryRepository(self.db).erase_by_user(
                tenant_id, end_user_id
            ),
            "session_tokens": await SessionTokenRepository(self.db).revoke_all_for_end_user(
                tenant_id, end_user_id
            ),
        }
        await EndUserRepository(self.db).set_status(
            tenant_id, end_user_id, "erased"
        )
        await AuditRepository(self.db).add(
            action="end_user.data_erased",
            resource_type="end_user",
            resource_id=end_user_id,
            tenant_id=tenant_id,
            actor_type="system",
            details={
                "deleted": deleted,
                "retained": ["audit_events", "end_user_identity"],
                "reason": "DSR erasure request",
                "at": datetime.now(timezone.utc).isoformat(),
            },
        )
        logger.info(
            "end_user_data_erased", tenant_id=tenant_id, end_user_id=end_user_id, **deleted
        )
        return deleted
