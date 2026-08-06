"""
Repository layer over the SQLAlchemy models.

Repositories take a DatabaseManager and open their own sessions, so they
work both inside a request and from fire-and-forget background tasks.
All methods return plain dicts to avoid leaking ORM objects.
"""

import structlog
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, func, select, update

from .manager import DatabaseManager
from .models import (
    ApiKeyModel,
    AuditEventModel,
    ConversationModel,
    EscalationModel,
    GuardrailEvidenceModel,
    MessageModel,
    ModelCatalogModel,
    PolicyRuleModel,
    PolicySetModel,
    TenantModel,
    WebhookDeliveryModel,
    WebhookEventModel,
    WebhookSubscriptionModel,
)

logger = structlog.get_logger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _row_to_dict(row: Any) -> dict[str, Any]:
    return {c.name: getattr(row, c.name) for c in row.__table__.columns}


class TenantRepository:
    def __init__(self, db: DatabaseManager):
        self.db = db

    async def create(self, data: dict[str, Any]) -> dict[str, Any]:
        async with self.db.get_session() as session:
            model = TenantModel(**data)
            session.add(model)
            await session.flush()
            return _row_to_dict(model)

    async def get_by_id(self, tenant_id: str) -> dict[str, Any] | None:
        async with self.db.get_session() as session:
            row = await session.get(TenantModel, tenant_id)
            return _row_to_dict(row) if row else None

    async def get_by_slug(self, slug: str) -> dict[str, Any] | None:
        async with self.db.get_session() as session:
            result = await session.execute(
                select(TenantModel).where(TenantModel.slug == slug)
            )
            row = result.scalar_one_or_none()
            return _row_to_dict(row) if row else None

    async def list_all(self) -> list[dict[str, Any]]:
        async with self.db.get_session() as session:
            result = await session.execute(select(TenantModel).order_by(TenantModel.created_at))
            return [_row_to_dict(r) for r in result.scalars()]

    async def update(self, tenant_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
        async with self.db.get_session() as session:
            updates = {k: v for k, v in updates.items() if v is not None}
            await session.execute(
                update(TenantModel)
                .where(TenantModel.id == tenant_id)
                .values(**updates)
            )
            row = await session.get(TenantModel, tenant_id)
            return _row_to_dict(row) if row else None

    async def delete(self, tenant_id: str) -> bool:
        async with self.db.get_session() as session:
            result = await session.execute(
                delete(TenantModel).where(TenantModel.id == tenant_id)
            )
            return result.rowcount > 0


class ConversationRepository:
    def __init__(self, db: DatabaseManager):
        self.db = db

    async def get_or_create(self, tenant_id: str, session_id: str) -> dict[str, Any]:
        async with self.db.get_session() as session:
            result = await session.execute(
                select(ConversationModel).where(
                    ConversationModel.tenant_id == tenant_id,
                    ConversationModel.session_id == session_id,
                )
            )
            row = result.scalar_one_or_none()
            if row:
                return _row_to_dict(row)
            model = ConversationModel(
                id=str(uuid4()), tenant_id=tenant_id, session_id=session_id
            )
            session.add(model)
            await session.flush()
            return _row_to_dict(model)

    async def add_message(
        self,
        conversation_id: str,
        role: str,
        content: str,
        redacted_content: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        async with self.db.get_session() as session:
            model = MessageModel(
                id=str(uuid4()),
                conversation_id=conversation_id,
                role=role,
                content=content,
                redacted_content=redacted_content,
                metadata=metadata or {},
            )
            session.add(model)
            await session.flush()
            return _row_to_dict(model)

    async def get_by_session(self, tenant_id: str, session_id: str) -> dict[str, Any] | None:
        async with self.db.get_session() as session:
            result = await session.execute(
                select(ConversationModel).where(
                    ConversationModel.tenant_id == tenant_id,
                    ConversationModel.session_id == session_id,
                )
            )
            row = result.scalar_one_or_none()
            return _row_to_dict(row) if row else None

    async def list_by_tenant(self, tenant_id: str, limit: int = 100) -> list[dict[str, Any]]:
        async with self.db.get_session() as session:
            result = await session.execute(
                select(ConversationModel)
                .where(ConversationModel.tenant_id == tenant_id)
                .order_by(ConversationModel.created_at.desc())
                .limit(limit)
            )
            return [_row_to_dict(r) for r in result.scalars()]

    async def list_messages(
        self, conversation_id: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Return the most recent messages of a conversation, oldest first."""
        async with self.db.get_session() as session:
            result = await session.execute(
                select(MessageModel)
                .where(MessageModel.conversation_id == conversation_id)
                .order_by(MessageModel.created_at.desc())
                .limit(limit)
            )
            rows = list(result.scalars())
            return [_row_to_dict(r) for r in reversed(rows)]

    async def mark_escalated(self, conversation_id: str) -> None:
        async with self.db.get_session() as session:
            await session.execute(
                update(ConversationModel)
                .where(ConversationModel.id == conversation_id)
                .values(is_escalated=True)
            )

    async def set_status(self, conversation_id: str, status: str) -> bool:
        """Set the conversation lifecycle status (e.g. ``paused``/``active``).

        Returns True when the conversation exists.
        """
        async with self.db.get_session() as session:
            result = await session.execute(
                update(ConversationModel)
                .where(ConversationModel.id == conversation_id)
                .values(status=status)
            )
            return (result.rowcount or 0) > 0

    async def list_messages_all(
        self, conversation_ids: list[str], limit: int = 10000
    ) -> list[dict[str, Any]]:
        """Return all messages for the given conversations, oldest first."""
        if not conversation_ids:
            return []
        async with self.db.get_session() as session:
            result = await session.execute(
                select(MessageModel)
                .where(MessageModel.conversation_id.in_(conversation_ids))
                .order_by(MessageModel.created_at)
                .limit(limit)
            )
            return [_row_to_dict(r) for r in result.scalars()]

    async def delete_by_tenant(self, tenant_id: str) -> int:
        """Delete conversations and their messages for a tenant (GDPR erasure)."""
        async with self.db.get_session() as session:
            conv_result = await session.execute(
                select(ConversationModel.id).where(
                    ConversationModel.tenant_id == tenant_id
                )
            )
            conversation_ids = list(conv_result.scalars())
            message_result = await session.execute(
                delete(MessageModel).where(
                    MessageModel.conversation_id.in_(conversation_ids)
                )
            )
            conv_deleted = await session.execute(
                delete(ConversationModel).where(
                    ConversationModel.tenant_id == tenant_id
                )
            )
            return (message_result.rowcount or 0) + (conv_deleted.rowcount or 0)


class EvidenceRepository:
    def __init__(self, db: DatabaseManager):
        self.db = db

    async def add(self, record: dict[str, Any]) -> None:
        async with self.db.get_session() as session:
            session.add(GuardrailEvidenceModel(id=str(uuid4()), **record))
            await session.flush()

    async def list_by_tenant(self, tenant_id: str, limit: int = 1000) -> list[dict[str, Any]]:
        async with self.db.get_session() as session:
            result = await session.execute(
                select(GuardrailEvidenceModel)
                .where(GuardrailEvidenceModel.tenant_id == tenant_id)
                .order_by(GuardrailEvidenceModel.created_at.desc())
                .limit(limit)
            )
            return [_row_to_dict(r) for r in result.scalars()]

    async def delete_by_tenant(self, tenant_id: str) -> int:
        async with self.db.get_session() as session:
            result = await session.execute(
                delete(GuardrailEvidenceModel).where(
                    GuardrailEvidenceModel.tenant_id == tenant_id
                )
            )
            return result.rowcount or 0


class AuditRepository:
    def __init__(self, db: DatabaseManager):
        self.db = db

    async def add(
        self,
        action: str,
        resource_type: str,
        resource_id: str | None = None,
        tenant_id: str | None = None,
        actor_type: str = "system",
        actor_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        async with self.db.get_session() as session:
            model = AuditEventModel(
                id=str(uuid4()),
                tenant_id=tenant_id,
                actor_type=actor_type,
                actor_id=actor_id,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                details=details or {},
            )
            session.add(model)
            await session.flush()
            return _row_to_dict(model)

    async def list_events(
        self, tenant_id: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        async with self.db.get_session() as session:
            stmt = select(AuditEventModel)
            if tenant_id:
                stmt = stmt.where(AuditEventModel.tenant_id == tenant_id)
            result = await session.execute(stmt.order_by(AuditEventModel.created_at.desc()).limit(limit))
            return [_row_to_dict(r) for r in result.scalars()]


class ApiKeyRepository:
    def __init__(self, db: DatabaseManager):
        self.db = db

    async def create(
        self,
        name: str,
        key_hash: str,
        prefix: str,
        role: str,
        tenant_id: str | None = None,
        scopes: list[str] | None = None,
        expires_at: datetime | None = None,
    ) -> dict[str, Any]:
        async with self.db.get_session() as session:
            model = ApiKeyModel(
                id=str(uuid4()),
                name=name,
                key_hash=key_hash,
                prefix=prefix,
                role=role,
                tenant_id=tenant_id,
                scopes=scopes or [],
                expires_at=expires_at,
            )
            session.add(model)
            await session.flush()
            return _row_to_dict(model)

    async def get_by_hash(self, key_hash: str) -> dict[str, Any] | None:
        async with self.db.get_session() as session:
            result = await session.execute(
                select(ApiKeyModel).where(ApiKeyModel.key_hash == key_hash)
            )
            row = result.scalar_one_or_none()
            return _row_to_dict(row) if row else None

    async def list_all(self) -> list[dict[str, Any]]:
        async with self.db.get_session() as session:
            result = await session.execute(
                select(ApiKeyModel).order_by(ApiKeyModel.created_at.desc())
            )
            return [_row_to_dict(r) for r in result.scalars()]

    async def revoke(self, key_id: str) -> bool:
        async with self.db.get_session() as session:
            result = await session.execute(
                update(ApiKeyModel)
                .where(ApiKeyModel.id == key_id)
                .values(revoked=True)
            )
            return result.rowcount > 0

    async def revoke_all_for_tenant(self, tenant_id: str) -> int:
        async with self.db.get_session() as session:
            result = await session.execute(
                update(ApiKeyModel)
                .where(
                    ApiKeyModel.tenant_id == tenant_id,
                    ApiKeyModel.revoked.is_(False),
                )
                .values(revoked=True)
            )
            return result.rowcount or 0

    async def touch_usage(self, key_id: str) -> None:
        async with self.db.get_session() as session:
            await session.execute(
                update(ApiKeyModel)
                .where(ApiKeyModel.id == key_id)
                .values(last_used_at=_now(), usage_count=ApiKeyModel.usage_count + 1)
            )

    async def count_active(self) -> int:
        async with self.db.get_session() as session:
            result = await session.execute(
                select(func.count())
                .select_from(ApiKeyModel)
                .where(ApiKeyModel.revoked.is_(False))
            )
            return int(result.scalar_one())


class EscalationRepository:
    def __init__(self, db: DatabaseManager):
        self.db = db

    async def add(self, record: dict[str, Any]) -> dict[str, Any]:
        async with self.db.get_session() as session:
            record = dict(record)
            record.setdefault("id", str(uuid4()))
            model = EscalationModel(**record)
            session.add(model)
            await session.flush()
            return _row_to_dict(model)

    async def list_by_tenant(
        self, tenant_id: str, status: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        async with self.db.get_session() as session:
            stmt = select(EscalationModel).where(EscalationModel.tenant_id == tenant_id)
            if status:
                stmt = stmt.where(EscalationModel.status == status)
            result = await session.execute(
                stmt.order_by(EscalationModel.created_at.desc()).limit(limit)
            )
            return [_row_to_dict(r) for r in result.scalars()]

    async def list_all(
        self, status: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        async with self.db.get_session() as session:
            stmt = select(EscalationModel)
            if status:
                stmt = stmt.where(EscalationModel.status == status)
            result = await session.execute(
                stmt.order_by(EscalationModel.created_at.desc()).limit(limit)
            )
            return [_row_to_dict(r) for r in result.scalars()]

    async def get_by_id(self, escalation_id: str) -> dict[str, Any] | None:
        async with self.db.get_session() as session:
            result = await session.execute(
                select(EscalationModel).where(EscalationModel.id == escalation_id)
            )
            row = result.scalar_one_or_none()
            return _row_to_dict(row) if row else None

    async def list_by_conversation(
        self, conversation_id: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        async with self.db.get_session() as session:
            result = await session.execute(
                select(EscalationModel)
                .where(EscalationModel.conversation_id == conversation_id)
                .order_by(EscalationModel.created_at.desc())
                .limit(limit)
            )
            return [_row_to_dict(r) for r in result.scalars()]

    async def update_status(
        self, escalation_id: str, status: str, **extra: Any
    ) -> dict[str, Any] | None:
        """Transition an escalation's status; returns the updated row or None."""
        async with self.db.get_session() as session:
            result = await session.execute(
                select(EscalationModel).where(EscalationModel.id == escalation_id)
            )
            row = result.scalar_one_or_none()
            if row is None:
                return None
            row.status = status
            for key, value in extra.items():
                setattr(row, key, value)
            await session.flush()
            return _row_to_dict(row)

    async def delete_by_tenant(self, tenant_id: str) -> int:
        async with self.db.get_session() as session:
            result = await session.execute(
                delete(EscalationModel).where(EscalationModel.tenant_id == tenant_id)
            )
            return result.rowcount or 0


class PolicyRepository:
    def __init__(self, db: DatabaseManager):
        self.db = db

    async def create_set(
        self,
        tenant_id: str,
        name: str,
        rules: list[dict[str, Any]],
        status: str = "published",
    ) -> dict[str, Any]:
        async with self.db.get_session() as session:
            model = PolicySetModel(
                id=str(uuid4()), tenant_id=tenant_id, name=name, status=status
            )
            session.add(model)
            await session.flush()
            for rule_data in rules:
                session.add(
                    PolicyRuleModel(
                        id=str(uuid4()),
                        policy_set_id=model.id,
                        name=rule_data.get("name", ""),
                        policy_type=rule_data.get("policy_type", "topic_filter"),
                        action=rule_data.get("action", "allow"),
                        conditions=rule_data.get("conditions", {}),
                        priority=rule_data.get("priority", 0),
                        enabled=rule_data.get("enabled", True),
                    )
                )
            await session.flush()
            return _row_to_dict(model)

    async def get_by_tenant(self, tenant_id: str) -> dict[str, Any] | None:
        async with self.db.get_session() as session:
            result = await session.execute(
                select(PolicySetModel)
                .where(PolicySetModel.tenant_id == tenant_id)
                .order_by(PolicySetModel.version.desc())
                .limit(1)
            )
            row = result.scalar_one_or_none()
            if not row:
                return None
            rules_result = await session.execute(
                select(PolicyRuleModel)
                .where(PolicyRuleModel.policy_set_id == row.id)
                .order_by(PolicyRuleModel.priority.desc())
            )
            data = _row_to_dict(row)
            data["rules"] = [_row_to_dict(r) for r in rules_result.scalars()]
            return data


class WebhookRepository:
    """Webhook subscriptions, published events and delivery attempts."""

    def __init__(self, db: DatabaseManager):
        self.db = db

    async def create_subscription(
        self,
        tenant_id: str,
        url: str,
        secret: str,
        events: list[str],
    ) -> dict[str, Any]:
        async with self.db.get_session() as session:
            model = WebhookSubscriptionModel(
                id=str(uuid4()),
                tenant_id=tenant_id,
                url=url,
                secret=secret,
                events=events,
                active=True,
            )
            session.add(model)
            await session.flush()
            return _row_to_dict(model)

    async def list_subscriptions(self, tenant_id: str) -> list[dict[str, Any]]:
        async with self.db.get_session() as session:
            result = await session.execute(
                select(WebhookSubscriptionModel)
                .where(WebhookSubscriptionModel.tenant_id == tenant_id)
                .order_by(WebhookSubscriptionModel.created_at)
            )
            return [_row_to_dict(r) for r in result.scalars()]

    async def get_subscription(
        self, subscription_id: str, tenant_id: str
    ) -> dict[str, Any] | None:
        async with self.db.get_session() as session:
            result = await session.execute(
                select(WebhookSubscriptionModel).where(
                    WebhookSubscriptionModel.id == subscription_id,
                    WebhookSubscriptionModel.tenant_id == tenant_id,
                )
            )
            row = result.scalar_one_or_none()
            return _row_to_dict(row) if row else None

    async def set_subscription_active(
        self, subscription_id: str, tenant_id: str, active: bool
    ) -> dict[str, Any] | None:
        async with self.db.get_session() as session:
            result = await session.execute(
                update(WebhookSubscriptionModel)
                .where(
                    WebhookSubscriptionModel.id == subscription_id,
                    WebhookSubscriptionModel.tenant_id == tenant_id,
                )
                .values(active=active)
                .returning(WebhookSubscriptionModel)
            )
            row = result.scalar_one_or_none()
            return _row_to_dict(row) if row else None

    async def delete_subscription(self, subscription_id: str, tenant_id: str) -> bool:
        async with self.db.get_session() as session:
            result = await session.execute(
                delete(WebhookSubscriptionModel).where(
                    WebhookSubscriptionModel.id == subscription_id,
                    WebhookSubscriptionModel.tenant_id == tenant_id,
                )
            )
            return bool(result.rowcount)

    async def record_event(
        self, event_id: str, tenant_id: str, event_type: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        async with self.db.get_session() as session:
            model = WebhookEventModel(
                id=event_id,
                tenant_id=tenant_id,
                event_type=event_type,
                payload=payload,
                delivered_to=[],
            )
            session.add(model)
            await session.flush()
            return _row_to_dict(model)

    async def get_event(self, event_id: str) -> dict[str, Any] | None:
        async with self.db.get_session() as session:
            result = await session.execute(
                select(WebhookEventModel).where(WebhookEventModel.id == event_id)
            )
            row = result.scalar_one_or_none()
            return _row_to_dict(row) if row else None

    async def list_events(
        self, tenant_id: str | None = None, event_type: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        async with self.db.get_session() as session:
            stmt = select(WebhookEventModel)
            if tenant_id:
                stmt = stmt.where(WebhookEventModel.tenant_id == tenant_id)
            if event_type:
                stmt = stmt.where(WebhookEventModel.event_type == event_type)
            result = await session.execute(
                stmt.order_by(WebhookEventModel.created_at.desc()).limit(limit)
            )
            return [_row_to_dict(r) for r in result.scalars()]

    async def mark_delivered(self, event_id: str, subscription_id: str) -> None:
        async with self.db.get_session() as session:
            model = await session.get(WebhookEventModel, event_id)
            if model is None:
                return
            delivered = list(model.delivered_to)
            if subscription_id not in delivered:
                delivered.append(subscription_id)
            model.delivered_to = delivered
            await session.flush()

    async def record_delivery(
        self,
        event_id: str,
        subscription_id: str,
        tenant_id: str,
        status: str,
        http_status: int | None = None,
        error: str | None = None,
        attempts: int = 1,
    ) -> dict[str, Any]:
        async with self.db.get_session() as session:
            model = WebhookDeliveryModel(
                id=str(uuid4()),
                event_id=event_id,
                subscription_id=subscription_id,
                tenant_id=tenant_id,
                status=status,
                http_status=http_status,
                error=error,
                attempts=attempts,
            )
            session.add(model)
            await session.flush()
            return _row_to_dict(model)

    async def list_deliveries(
        self, event_id: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        async with self.db.get_session() as session:
            stmt = select(WebhookDeliveryModel)
            if event_id:
                stmt = stmt.where(WebhookDeliveryModel.event_id == event_id)
            result = await session.execute(
                stmt.order_by(WebhookDeliveryModel.created_at.desc()).limit(limit)
            )
            return [_row_to_dict(r) for r in result.scalars()]

    async def delete_by_tenant(self, tenant_id: str) -> int:
        """Purge subscriptions, events and deliveries for a tenant (GDPR erasure)."""
        async with self.db.get_session() as session:
            sub_deleted = await session.execute(
                delete(WebhookSubscriptionModel).where(
                    WebhookSubscriptionModel.tenant_id == tenant_id
                )
            )
            event_ids = list(
                (
                    await session.execute(
                        select(WebhookEventModel.id).where(
                            WebhookEventModel.tenant_id == tenant_id
                        )
                    )
                ).scalars()
            )
            delivery_deleted = 0
            if event_ids:
                delivery_deleted = (
                    await session.execute(
                        delete(WebhookDeliveryModel).where(
                            WebhookDeliveryModel.event_id.in_(event_ids)
                        )
                    )
                ).rowcount or 0
            event_deleted = await session.execute(
                delete(WebhookEventModel).where(
                    WebhookEventModel.tenant_id == tenant_id
                )
            )
            return (
                (sub_deleted.rowcount or 0)
                + (event_deleted.rowcount or 0)
                + delivery_deleted
            )


class ModelCatalogRepository:
    """Per-tenant model catalog (matrix 5.2)."""

    def __init__(self, db: DatabaseManager):
        self.db = db

    async def create(
        self,
        tenant_id: str,
        provider: str,
        model: str,
        cost_ceiling_per_1k: float = 0.0,
        fallback_order: int = 0,
    ) -> dict[str, Any]:
        async with self.db.get_session() as session:
            model_row = ModelCatalogModel(
                id=str(uuid4()),
                tenant_id=tenant_id,
                provider=provider,
                model=model,
                cost_ceiling_per_1k=cost_ceiling_per_1k,
                fallback_order=fallback_order,
            )
            session.add(model_row)
            await session.flush()
            return _row_to_dict(model_row)

    async def list_by_tenant(self, tenant_id: str) -> list[dict[str, Any]]:
        async with self.db.get_session() as session:
            result = await session.execute(
                select(ModelCatalogModel)
                .where(ModelCatalogModel.tenant_id == tenant_id)
                .order_by(ModelCatalogModel.fallback_order, ModelCatalogModel.model)
            )
            return [_row_to_dict(r) for r in result.scalars()]

    async def set_enabled(self, entry_id: str, tenant_id: str, enabled: bool) -> dict[str, Any] | None:
        async with self.db.get_session() as session:
            result = await session.execute(
                update(ModelCatalogModel)
                .where(
                    ModelCatalogModel.id == entry_id,
                    ModelCatalogModel.tenant_id == tenant_id,
                )
                .values(enabled=enabled)
                .returning(ModelCatalogModel)
            )
            row = result.scalar_one_or_none()
            return _row_to_dict(row) if row else None

    async def delete(self, entry_id: str, tenant_id: str) -> bool:
        async with self.db.get_session() as session:
            result = await session.execute(
                delete(ModelCatalogModel).where(
                    ModelCatalogModel.id == entry_id,
                    ModelCatalogModel.tenant_id == tenant_id,
                )
            )
            return bool(result.rowcount)
