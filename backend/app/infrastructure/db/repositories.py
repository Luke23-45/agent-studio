"""
Repository layer over the SQLAlchemy models.

Repositories take a DatabaseManager and open their own sessions, so they
work both inside a request and from fire-and-forget background tasks.
All methods return plain dicts to avoid leaking ORM objects.
"""

import hashlib
import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import structlog
from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError

from .manager import DatabaseManager
from .models import (
    ApiKeyModel,
    AuditEventModel,
    CacheInvalidationLogModel,
    ConversationModel,
    EndUserModel,
    EscalationModel,
    GuardrailEvidenceModel,
    MessageModel,
    ModelCatalogModel,
    PolicyRuleModel,
    PolicySetModel,
    QuotaStateModel,
    SessionTokenModel,
    SpendEventModel,
    SurfaceModel,
    TenantConfigVersionModel,
    TenantModel,
    TenantProviderKeyModel,
    ToolRegistryModel,
    WebhookDeliveryModel,
    WebhookEventModel,
    WebhookSubscriptionModel,
)

logger = structlog.get_logger(__name__)

from backend.app.governance.promotion import VALIDATION_REGRESSED, canary_bucket


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _row_to_dict(row: Any) -> dict[str, Any]:
    return {
        attr.key: getattr(row, attr.key)
        for attr in row.__mapper__.column_attrs
    }


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
                data = _row_to_dict(row)
                data["created"] = False
                return data
            model = ConversationModel(
                id=str(uuid4()), tenant_id=tenant_id, session_id=session_id
            )
            session.add(model)
            await session.flush()
            data = _row_to_dict(model)
            data["created"] = True
            return data

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

    async def list_messages_by_end_user(
        self, tenant_id: str, end_user_id: str, limit: int = 10000
    ) -> list[dict[str, Any]]:
        """Return the messages authored by one end user (DSR export)."""
        async with self.db.get_session() as session:
            result = await session.execute(
                select(MessageModel)
                .where(
                    MessageModel.tenant_id == tenant_id,
                    MessageModel.end_user_id == end_user_id,
                )
                .order_by(MessageModel.created_at)
                .limit(limit)
            )
            return [_row_to_dict(r) for r in result.scalars()]

    async def delete_messages_by_end_user(
        self, tenant_id: str, end_user_id: str
    ) -> int:
        """Hard-delete messages authored by one end user (DSR erasure)."""
        async with self.db.get_session() as session:
            result = await session.execute(
                delete(MessageModel).where(
                    MessageModel.tenant_id == tenant_id,
                    MessageModel.end_user_id == end_user_id,
                )
            )
            return result.rowcount or 0


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
    """Immutable, tamper-evident admin audit trail (P5-9).

    Rows are append-only: there is no update/delete surface. Every row
    stores ``event_hash`` (SHA-256 of the previous hash + this row's
    canonical content); ``verify_chain`` recomputes the chain so tampering
    is detected. Rows predating the chain schema carry NULL hashes and are
    treated as unverifiable legacy entries (the chain starts at the first
    hashed row).
    """

    def __init__(self, db: DatabaseManager):
        self.db = db

    @staticmethod
    def _canonical_json(value: Any) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _utc_iso(value: datetime) -> str:
        """Canonical instant: tz-normalized so an aware insert-time datetime
        and its naive (SQLite) read-back produce the same digest."""
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()

    def _compute_hash(
        self,
        *,
        prev_hash: str | None,
        event_id: str,
        tenant_id: str | None,
        actor_type: str,
        actor_id: str | None,
        action: str,
        resource_type: str,
        resource_id: str | None,
        details: dict[str, Any],
        created_at: datetime,
    ) -> str:
        parts = [
            prev_hash or "",
            event_id,
            tenant_id or "",
            actor_type,
            actor_id or "",
            action,
            resource_type,
            resource_id or "",
            self._canonical_json(details),
            self._utc_iso(created_at),
        ]
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()

    async def _previous_hash(self, event_id: str, created_at: datetime) -> str | None:
        """Hash of the immediate predecessor in canonical (created_at, id)
        order — the same order ``verify_chain`` walks, so timestamp ties can
        never desynchronize the chain."""
        async with self.db.get_session() as session:
            result = await session.execute(
                select(AuditEventModel.event_hash)
                .where(
                    or_(
                        AuditEventModel.created_at < created_at,
                        and_(
                            AuditEventModel.created_at == created_at,
                            AuditEventModel.id < event_id,
                        ),
                    )
                )
                .order_by(AuditEventModel.created_at.desc(), AuditEventModel.id.desc())
                .limit(1)
            )
            return result.scalar_one_or_none()

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
        event_id = str(uuid4())
        created_at = _now()
        prev_hash = await self._previous_hash(event_id, created_at)
        event_hash = self._compute_hash(
            prev_hash=prev_hash,
            event_id=event_id,
            tenant_id=tenant_id,
            actor_type=actor_type,
            actor_id=actor_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            details=details or {},
            created_at=created_at,
        )
        async with self.db.get_session() as session:
            model = AuditEventModel(
                id=event_id,
                tenant_id=tenant_id,
                actor_type=actor_type,
                actor_id=actor_id,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                details=details or {},
                prev_hash=prev_hash,
                event_hash=event_hash,
                created_at=created_at,
            )
            session.add(model)
            await session.flush()
            return _row_to_dict(model)

    async def list_events(
        self,
        tenant_id: str | None = None,
        limit: int = 100,
        action: str | None = None,
    ) -> list[dict[str, Any]]:
        async with self.db.get_session() as session:
            stmt = select(AuditEventModel)
            if tenant_id:
                stmt = stmt.where(AuditEventModel.tenant_id == tenant_id)
            if action:
                stmt = stmt.where(AuditEventModel.action == action)
            result = await session.execute(stmt.order_by(AuditEventModel.created_at.desc()).limit(limit))
            return [_row_to_dict(r) for r in result.scalars()]

    async def get_by_id(self, event_id: str) -> dict[str, Any] | None:
        async with self.db.get_session() as session:
            result = await session.execute(
                select(AuditEventModel).where(AuditEventModel.id == event_id)
            )
            row = result.scalar_one_or_none()
            return _row_to_dict(row) if row else None

    async def verify_chain(self, tenant_id: str | None = None) -> tuple[bool, str | None]:
        """Recompute the hash chain over the trail.

        Returns ``(ok, broken_at)``: ``ok`` is False when a hashed row does
        not match its recomputed hash; ``broken_at`` is the id of the first
        bad row (None when the chain is intact or has no hashed rows).
        """
        async with self.db.get_session() as session:
            stmt = select(AuditEventModel)
            if tenant_id:
                stmt = stmt.where(AuditEventModel.tenant_id == tenant_id)
            rows = (
                await session.execute(
                    stmt.order_by(AuditEventModel.created_at.asc(), AuditEventModel.id.asc())
                )
            ).scalars().all()

        prev_hash: str | None = None
        for row in rows:
            if row.event_hash is None:
                # legacy row: predates the chain schema; does not break it,
                # but a chained row must never follow an unverified one.
                prev_hash = None
                continue
            expected = self._compute_hash(
                prev_hash=prev_hash,
                event_id=row.id,
                tenant_id=row.tenant_id,
                actor_type=row.actor_type,
                actor_id=row.actor_id,
                action=row.action,
                resource_type=row.resource_type,
                resource_id=row.resource_id,
                details=row.details,
                created_at=row.created_at,
            )
            if row.event_hash != expected:
                return False, row.id
            prev_hash = row.event_hash
        return True, None


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


class SpendEventRepository:
    """Append-only usage/spend events (Arch 10 cost ledger, P0-10)."""

    def __init__(self, db: DatabaseManager):
        self.db = db

    async def add(self, record: dict[str, Any]) -> dict[str, Any]:
        record = dict(record)
        record.setdefault("id", str(uuid4()))
        async with self.db.get_session() as session:
            model = SpendEventModel(**record)
            session.add(model)
            await session.flush()
            return _row_to_dict(model)

    async def list_filtered(
        self,
        tenant_id: str | None = None,
        limit: int = 100,
        conversation_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Newest-first spend events (P0-10 cost ledger), optionally scoped
        to a tenant and/or conversation."""
        async with self.db.get_session() as session:
            stmt = select(SpendEventModel)
            if tenant_id:
                stmt = stmt.where(SpendEventModel.tenant_id == tenant_id)
            if conversation_id:
                stmt = stmt.where(
                    SpendEventModel.conversation_id == conversation_id
                )
            result = await session.execute(
                stmt.order_by(SpendEventModel.created_at.desc()).limit(limit)
            )
            return [_row_to_dict(r) for r in result.scalars()]

    async def get_by_id(
        self, event_id: str, tenant_id: str | None = None
    ) -> dict[str, Any] | None:
        async with self.db.get_session() as session:
            stmt = select(SpendEventModel).where(SpendEventModel.id == event_id)
            if tenant_id:
                stmt = stmt.where(SpendEventModel.tenant_id == tenant_id)
            row = (await session.execute(stmt)).scalar_one_or_none()
            return _row_to_dict(row) if row else None


class QuotaStateRepository:
    """Durable USD quota state (Arch 10, P3-6) — one row per
    (scope_type, tenant, surface, end_user, window).

    Written by the cost-ledger consumer (``cost_ledger.write`` handler)
    from the same spend event that feeds ``spend_events``, so a window's
    spend is reconstructable from durable rows even if Redis state is
    lost. This repository only appends spent amounts; reservations live
    in the Redis fast path (``gateway.quota``).
    """

    def __init__(self, db: DatabaseManager):
        self.db = db

    async def record_spend(
        self,
        *,
        scope_type: str,
        tenant_id: str,
        surface_id: str | None,
        end_user_id: str | None,
        window_started_at: datetime,
        spent_usd: float,
    ) -> dict[str, Any]:
        """Upsert: add ``spent_usd`` to the scope's window row."""
        async with self.db.get_session() as session:
            result = await session.execute(
                select(QuotaStateModel).where(
                    QuotaStateModel.scope_type == scope_type,
                    QuotaStateModel.tenant_id == tenant_id,
                    QuotaStateModel.surface_id == surface_id,
                    QuotaStateModel.end_user_id == end_user_id,
                    QuotaStateModel.window_started_at == window_started_at,
                )
            )
            row = result.scalar_one_or_none()
            if row is None:
                row = QuotaStateModel(
                    id=str(uuid4()),
                    scope_type=scope_type,
                    tenant_id=tenant_id,
                    surface_id=surface_id,
                    end_user_id=end_user_id,
                    window_started_at=window_started_at,
                    spent_usd=spent_usd,
                    reserved_usd=0.0,
                )
                session.add(row)
            else:
                row.spent_usd += spent_usd
            await session.flush()
            return _row_to_dict(row)

    async def get_window(
        self,
        *,
        scope_type: str,
        tenant_id: str,
        surface_id: str | None,
        end_user_id: str | None,
        window_started_at: datetime,
    ) -> dict[str, Any] | None:
        async with self.db.get_session() as session:
            result = await session.execute(
                select(QuotaStateModel).where(
                    QuotaStateModel.scope_type == scope_type,
                    QuotaStateModel.tenant_id == tenant_id,
                    QuotaStateModel.surface_id == surface_id,
                    QuotaStateModel.end_user_id == end_user_id,
                    QuotaStateModel.window_started_at == window_started_at,
                )
            )
            row = result.scalar_one_or_none()
            return _row_to_dict(row) if row else None


class CacheInvalidationRepository:
    """Durable record of cache invalidations (Arch 10, P3-7)."""

    def __init__(self, db: DatabaseManager):
        self.db = db

    async def add(
        self,
        *,
        tenant_id: str,
        scope: str,
        resource_id: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        async with self.db.get_session() as session:
            row = CacheInvalidationLogModel(
                id=str(uuid4()),
                tenant_id=tenant_id,
                scope=scope,
                resource_id=resource_id,
                reason=reason,
            )
            session.add(row)
            await session.flush()
            return _row_to_dict(row)

    async def list_recent(self, tenant_id: str, limit: int = 50) -> list[dict[str, Any]]:
        async with self.db.get_session() as session:
            result = await session.execute(
                select(CacheInvalidationLogModel)
                .where(CacheInvalidationLogModel.tenant_id == tenant_id)
                .order_by(CacheInvalidationLogModel.created_at.desc())
                .limit(limit)
            )
            return [_row_to_dict(r) for r in result.scalars()]


class TenantConfigVersionRepository:
    """Immutable, versioned tenant config (Arch 12, P0-11).

    Versions are append-only: creating a draft writes a new row and
    promoting one publishes it (superseding any previously published
    version). The runtime reads the latest published version.
    """

    STATUS_DRAFT = "draft"
    STATUS_PUBLISHED = "published"
    STATUS_SUPERSEDED = "superseded"

    def __init__(self, db: DatabaseManager):
        self.db = db

    async def list_versions(self, tenant_id: str) -> list[dict[str, Any]]:
        async with self.db.get_session() as session:
            result = await session.execute(
                select(TenantConfigVersionModel)
                .where(TenantConfigVersionModel.tenant_id == tenant_id)
                .order_by(TenantConfigVersionModel.version.desc())
            )
            return [_row_to_dict(r) for r in result.scalars()]

    async def get_latest_published(self, tenant_id: str) -> dict[str, Any] | None:
        async with self.db.get_session() as session:
            result = await session.execute(
                select(TenantConfigVersionModel)
                .where(
                    TenantConfigVersionModel.tenant_id == tenant_id,
                    TenantConfigVersionModel.status == self.STATUS_PUBLISHED,
                )
                .order_by(TenantConfigVersionModel.version.desc())
                .limit(1)
            )
            row = result.scalar_one_or_none()
            return _row_to_dict(row) if row else None

    async def create_draft(
        self, tenant_id: str, config: dict[str, Any], promoted_by: str | None = None
    ) -> dict[str, Any]:
        """Append a new immutable draft (version = latest + 1)."""
        async with self.db.get_session() as session:
            result = await session.execute(
                select(func.max(TenantConfigVersionModel.version)).where(
                    TenantConfigVersionModel.tenant_id == tenant_id
                )
            )
            next_version = (result.scalar() or 0) + 1
            model = TenantConfigVersionModel(
                id=str(uuid4()),
                tenant_id=tenant_id,
                version=next_version,
                config=config,
                status=self.STATUS_DRAFT,
                promoted_by=promoted_by,
            )
            session.add(model)
            await session.flush()
            return _row_to_dict(model)

    async def get(self, tenant_id: str, version: int) -> dict[str, Any] | None:
        """Return one version by number (any status)."""
        async with self.db.get_session() as session:
            result = await session.execute(
                select(TenantConfigVersionModel).where(
                    TenantConfigVersionModel.tenant_id == tenant_id,
                    TenantConfigVersionModel.version == version,
                )
            )
            row = result.scalar_one_or_none()
            return _row_to_dict(row) if row else None

    async def set_validation(
        self,
        tenant_id: str,
        version: int,
        validation_status: str,
        by: str | None = None,
    ) -> dict[str, Any] | None:
        """Record the pipeline evaluation result (P5-2).

        ``validation_status`` is one of "" (not yet), "validated" (schema +
        compile passed) or "failed". Passing also stores the approver --
        the ``by`` is the acting principal. The `approval` step is a second
        explicit call (approval is separate from the eval gate).
        """
        async with self.db.get_session() as session:
            result = await session.execute(
                select(TenantConfigVersionModel).where(
                    TenantConfigVersionModel.tenant_id == tenant_id,
                    TenantConfigVersionModel.version == version,
                )
            )
            row = result.scalar_one_or_none()
            if row is None:
                return None
            row.validation_status = validation_status
            row.validated_at = _now()
            _ = by  # caller records the audit event; column is promoted_by
            await session.flush()
            return _row_to_dict(row)

    async def approve(
        self, tenant_id: str, version: int, approved_by: str | None = None
    ) -> dict[str, Any] | None:
        """Record privileged-change approval (P5-2 approvals, audited)."""
        async with self.db.get_session() as session:
            result = await session.execute(
                select(TenantConfigVersionModel).where(
                    TenantConfigVersionModel.tenant_id == tenant_id,
                    TenantConfigVersionModel.version == version,
                )
            )
            row = result.scalar_one_or_none()
            if row is None:
                return None
            row.approved_by = approved_by
            row.approved_at = _now()
            await session.flush()
            return _row_to_dict(row)

    async def promote(
        self,
        tenant_id: str,
        version: int,
        promoted_by: str | None = None,
        canary_percent: int | None = None,
    ) -> dict[str, Any] | None:
        """Publish/rollback: promote a version, supersede the previous one.

        A canary rollout (``0 < canary_percent < 100``) keeps the currently
        published version live as the baseline so ``get_effective`` can split
        request traffic deterministically; a full rollout (100/None) supersedes
        the previous published version outright.
        """
        async with self.db.get_session() as session:
            result = await session.execute(
                select(TenantConfigVersionModel).where(
                    TenantConfigVersionModel.tenant_id == tenant_id,
                    TenantConfigVersionModel.version == version,
                )
            )
            row = result.scalar_one_or_none()
            if row is None:
                return None
            if canary_percent is None or canary_percent >= 100:
                # Full rollout: the previous published version is superseded.
                await session.execute(
                    update(TenantConfigVersionModel)
                    .where(
                        TenantConfigVersionModel.tenant_id == tenant_id,
                        TenantConfigVersionModel.status == self.STATUS_PUBLISHED,
                    )
                    .values(status=self.STATUS_SUPERSEDED)
                )
            # Canary rollout: baseline is left published; it is the version
            # get_previous_published() picks up for the non-canary slice.
            row.status = self.STATUS_PUBLISHED
            row.published_at = _now()
            row.promoted_by = promoted_by
            if canary_percent is not None and canary_percent < 100:
                row.canary_percent = canary_percent
            else:
                row.canary_percent = None
            await session.flush()
            return _row_to_dict(row)

    async def get_previous_published(
        self, tenant_id: str, before_version: int
    ) -> dict[str, Any] | None:
        """The version that was live before ``before_version`` was promoted.

        Baseline for canary rollouts: with a canary active, the previous
        published row is kept (not superseded), so it is discoverable here.
        After a full rollout it is superseded and also returned, enabling
        auto-rollback after a full publish too.
        """
        async with self.db.get_session() as session:
            result = await session.execute(
                select(TenantConfigVersionModel)
                .where(
                    TenantConfigVersionModel.tenant_id == tenant_id,
                    TenantConfigVersionModel.status.in_(
                        [self.STATUS_PUBLISHED, self.STATUS_SUPERSEDED]
                    ),
                    TenantConfigVersionModel.version != before_version,
                )
                .order_by(TenantConfigVersionModel.version.desc())
                .limit(1)
            )
            row = result.scalar_one_or_none()
            return _row_to_dict(row) if row else None

    async def get_effective(
        self, tenant_id: str, request_key: str | None = None
    ) -> dict[str, Any] | None:
        """Runtime read: the published version to serve for ``request_key``.

        A canary rollout (<100) serves the canary to the deterministic slice
        (P5-2 ``canary_bucket``) and the previous published version to the
        remainder. Without a key (background workers) or at full rollout the
        latest published version is always returned.
        """
        latest = await self.get_latest_published(tenant_id)
        if latest is None:
            return None
        percent = latest.get("canary_percent")
        if percent is None or percent >= 100:
            return latest
        if request_key and not canary_bucket(request_key, percent):
            baseline = await self.get_previous_published(tenant_id, latest["version"])
            if baseline:
                return baseline
        return latest

    async def set_eval_result(
        self,
        tenant_id: str,
        version: int,
        eval_status: str,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Record the eval-suite result (P6-6 slot) on a draft.

        A failing suite blocks promotion at the publish gate; details carry
        per-metric evidence for the audit trail.
        """
        async with self.db.get_session() as session:
            result = await session.execute(
                select(TenantConfigVersionModel).where(
                    TenantConfigVersionModel.tenant_id == tenant_id,
                    TenantConfigVersionModel.version == version,
                )
            )
            row = result.scalar_one_or_none()
            if row is None:
                return None
            row.eval_status = eval_status
            row.eval_details = details
            await session.flush()
            return _row_to_dict(row)

    async def auto_rollback(
        self,
        tenant_id: str,
        failing_version: int,
        reason: str,
        rolled_back_by: str | None = None,
    ) -> dict[str, Any] | None:
        """Regression-triggered rollback (P5-2): re-promote the prior version.

        Marks the failing version ``validation_status="regressed"`` (a failed
        eval gate — it can never be promoted again until re-validated) and
        records the regression evidence, then does a full rollout of the
        previously live version.
        """
        async with self.db.get_session() as session:
            failing = (
                await session.execute(
                    select(TenantConfigVersionModel).where(
                        TenantConfigVersionModel.tenant_id == tenant_id,
                        TenantConfigVersionModel.version == failing_version,
                    )
                )
            ).scalar_one_or_none()
            if failing is None or failing.status != self.STATUS_PUBLISHED:
                return None
            previous_row = await session.execute(
                select(TenantConfigVersionModel)
                .where(
                    TenantConfigVersionModel.tenant_id == tenant_id,
                    TenantConfigVersionModel.status.in_(
                        [self.STATUS_PUBLISHED, self.STATUS_SUPERSEDED]
                    ),
                    TenantConfigVersionModel.version != failing_version,
                )
                .order_by(TenantConfigVersionModel.version.desc())
                .limit(1)
            )
            previous = previous_row.scalar_one_or_none()
            if previous is None:
                return None
            failing.validation_status = VALIDATION_REGRESSED
            failing.eval_details = {
                **(failing.eval_details or {}),
                "regression": reason,
                "auto_rolled_back_at": _now().isoformat(),
            }
            await session.execute(
                update(TenantConfigVersionModel)
                .where(
                    TenantConfigVersionModel.tenant_id == tenant_id,
                    TenantConfigVersionModel.status == self.STATUS_PUBLISHED,
                )
                .values(status=self.STATUS_SUPERSEDED)
            )
            previous.status = self.STATUS_PUBLISHED
            previous.published_at = _now()
            previous.canary_percent = None
            if rolled_back_by:
                previous.promoted_by = rolled_back_by
            await session.flush()
            return _row_to_dict(previous)


class TenantProviderKeyRepository:
    """Per-tenant provider credentials (Arch 6.3.9, P0-8).

    Encrypted secrets only; decryption happens in the keys service.
    ``upsert`` bumps ``key_version`` (rotation) and never returns the secret.
    """

    def __init__(self, db: DatabaseManager):
        self.db = db

    async def get(self, tenant_id: str, provider: str) -> dict[str, Any] | None:
        async with self.db.get_session() as session:
            result = await session.execute(
                select(TenantProviderKeyModel).where(
                    TenantProviderKeyModel.tenant_id == tenant_id,
                    TenantProviderKeyModel.provider == provider,
                )
            )
            row = result.scalar_one_or_none()
            return _row_to_dict(row) if row else None

    async def upsert(
        self,
        tenant_id: str,
        provider: str,
        encrypted_key: str,
        key_source: str,
        kms_ref: str | None = None,
    ) -> dict[str, Any]:
        async with self.db.get_session() as session:
            result = await session.execute(
                select(TenantProviderKeyModel).where(
                    TenantProviderKeyModel.tenant_id == tenant_id,
                    TenantProviderKeyModel.provider == provider,
                )
            )
            row = result.scalar_one_or_none()
            if row:
                row.encrypted_key = encrypted_key
                row.kms_ref = kms_ref
                row.key_source = key_source
                row.key_version = row.key_version + 1
                await session.flush()
                return _row_to_dict(row)
            model = TenantProviderKeyModel(
                id=str(uuid4()),
                tenant_id=tenant_id,
                provider=provider,
                encrypted_key=encrypted_key,
                kms_ref=kms_ref,
                key_source=key_source,
                key_version=1,
            )
            session.add(model)
            await session.flush()
            return _row_to_dict(model)

    async def delete(self, tenant_id: str, provider: str) -> bool:
        async with self.db.get_session() as session:
            result = await session.execute(
                delete(TenantProviderKeyModel).where(
                    TenantProviderKeyModel.tenant_id == tenant_id,
                    TenantProviderKeyModel.provider == provider,
                )
            )
            return result.rowcount > 0


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


class EndUserRepository:
    """End-user identity rows (Arch 6.4, P1-8). Identity never crosses tenants."""

    def __init__(self, db: DatabaseManager):
        self.db = db

    async def get_by_id(self, tenant_id: str, end_user_id: str) -> dict[str, Any] | None:
        async with self.db.get_session() as session:
            result = await session.execute(
                select(EndUserModel).where(
                    EndUserModel.tenant_id == tenant_id,
                    EndUserModel.id == end_user_id,
                )
            )
            row = result.scalar_one_or_none()
            return _row_to_dict(row) if row else None

    async def get_or_create_anonymous(
        self,
        tenant_id: str,
        anonymous_identity: str,
        display_name: str | None = None,
    ) -> dict[str, Any]:
        async with self.db.get_session() as session:
            result = await session.execute(
                select(EndUserModel).where(
                    EndUserModel.tenant_id == tenant_id,
                    EndUserModel.anonymous_identity == anonymous_identity,
                )
            )
            row = result.scalar_one_or_none()
            if row is not None:
                return _row_to_dict(row)
            model = EndUserModel(
                id=str(uuid4()),
                tenant_id=tenant_id,
                anonymous_identity=anonymous_identity,
                display_name=display_name,
                status="active",
            )
            session.add(model)
            try:
                await session.flush()
            except IntegrityError:
                await session.rollback()
                result = await session.execute(
                    select(EndUserModel).where(
                        EndUserModel.tenant_id == tenant_id,
                        EndUserModel.anonymous_identity == anonymous_identity,
                    )
                )
                row = result.scalar_one_or_none()
                if row is None:
                    raise
                return _row_to_dict(row)
            return _row_to_dict(model)

    async def create_authenticated(
        self,
        tenant_id: str,
        external_id: str,
        display_name: str | None = None,
    ) -> dict[str, Any]:
        """Register a tenant-issued end user (authenticated exchange, P1-8)."""
        async with self.db.get_session() as session:
            model = EndUserModel(
                id=str(uuid4()),
                tenant_id=tenant_id,
                external_id=external_id,
                display_name=display_name,
                status="active",
            )
            session.add(model)
            await session.flush()
            return _row_to_dict(model)

    async def list_by_tenant(self, tenant_id: str, limit: int = 1000) -> list[dict[str, Any]]:
        async with self.db.get_session() as session:
            result = await session.execute(
                select(EndUserModel)
                .where(EndUserModel.tenant_id == tenant_id)
                .order_by(EndUserModel.created_at.desc())
                .limit(limit)
            )
            return [_row_to_dict(r) for r in result.scalars()]

    async def set_status(
        self, tenant_id: str, end_user_id: str, status: str
    ) -> bool:
        """Mark an end user's durable status (e.g. ``erased``/``suspended``)."""
        async with self.db.get_session() as session:
            result = await session.execute(
                update(EndUserModel)
                .where(
                    EndUserModel.tenant_id == tenant_id,
                    EndUserModel.id == end_user_id,
                )
                .values(status=status)
            )
            return (result.rowcount or 0) > 0


class SessionTokenRepository:
    """Durable session-token registry (Arch 6.4, P1-8)."""

    def __init__(self, db: DatabaseManager):
        self.db = db

    async def create(
        self,
        jti: str,
        tenant_id: str,
        end_user_id: str,
        *,
        surface_id: str | None = None,
        device_id: str | None = None,
        scopes: list[str] | None = None,
        expires_at: datetime,
    ) -> dict[str, Any]:
        async with self.db.get_session() as session:
            model = SessionTokenModel(
                id=jti,
                tenant_id=tenant_id,
                end_user_id=end_user_id,
                surface_id=surface_id,
                device_id=device_id,
                scopes=scopes or [],
                expires_at=expires_at,
            )
            session.add(model)
            await session.flush()
            return _row_to_dict(model)

    async def get(self, jti: str) -> dict[str, Any] | None:
        async with self.db.get_session() as session:
            result = await session.execute(
                select(SessionTokenModel).where(SessionTokenModel.id == jti)
            )
            row = result.scalar_one_or_none()
            return _row_to_dict(row) if row else None

    async def revoke(self, jti: str) -> bool:
        async with self.db.get_session() as session:
            result = await session.execute(
                update(SessionTokenModel)
                .where(SessionTokenModel.id == jti, SessionTokenModel.revoked_at.is_(None))
                .values(revoked_at=_now())
            )
            return (result.rowcount or 0) > 0

    async def prune_expired(self, tenant_id: str, before: datetime) -> int:
        async with self.db.get_session() as session:
            result = await session.execute(
                delete(SessionTokenModel).where(
                    SessionTokenModel.tenant_id == tenant_id,
                    SessionTokenModel.expires_at < before,
                    SessionTokenModel.revoked_at.is_not(None),
                )
            )
            return result.rowcount or 0

    async def revoke_all_for_end_user(self, tenant_id: str, end_user_id: str) -> int:
        """Revoke every live session token for one end user (DSR erasure)."""
        async with self.db.get_session() as session:
            result = await session.execute(
                update(SessionTokenModel)
                .where(
                    SessionTokenModel.tenant_id == tenant_id,
                    SessionTokenModel.end_user_id == end_user_id,
                    SessionTokenModel.revoked_at.is_(None),
                )
                .values(revoked_at=_now())
            )
            return result.rowcount or 0


class SurfaceRepository:
    """Per-tenant surfaces (Arch 6.1, P1-9). Governance-owned rows."""

    def __init__(self, db: DatabaseManager):
        self.db = db

    async def create(
        self,
        tenant_id: str,
        name: str,
        *,
        surface_type: str = "widget",
        persona: str | None = None,
        knowledge_allowlist: list[str] | None = None,
        tool_allowlist: list[str] | None = None,
        model_pin: str | None = None,
        budgets: dict[str, Any] | None = None,
        brand_voice_override: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        async with self.db.get_session() as session:
            model = SurfaceModel(
                id=str(uuid4()),
                tenant_id=tenant_id,
                name=name,
                surface_type=surface_type,
                persona=persona,
                knowledge_allowlist=knowledge_allowlist or [],
                tool_allowlist=tool_allowlist or [],
                model_pin=model_pin,
                budgets=budgets or {},
                brand_voice_override=brand_voice_override,
                active=True,
            )
            session.add(model)
            await session.flush()
            return _row_to_dict(model)

    async def get(self, tenant_id: str, surface_id: str) -> dict[str, Any] | None:
        async with self.db.get_session() as session:
            result = await session.execute(
                select(SurfaceModel).where(
                    SurfaceModel.tenant_id == tenant_id,
                    SurfaceModel.id == surface_id,
                )
            )
            row = result.scalar_one_or_none()
            return _row_to_dict(row) if row else None

    async def get_default(self, tenant_id: str) -> dict[str, Any] | None:
        """First active surface (tenant onboarding default)."""
        async with self.db.get_session() as session:
            result = await session.execute(
                select(SurfaceModel)
                .where(SurfaceModel.tenant_id == tenant_id, SurfaceModel.active.is_(True))
                .order_by(SurfaceModel.created_at.asc())
                .limit(1)
            )
            row = result.scalar_one_or_none()
            return _row_to_dict(row) if row else None

    async def list_by_tenant(self, tenant_id: str) -> list[dict[str, Any]]:
        async with self.db.get_session() as session:
            result = await session.execute(
                select(SurfaceModel)
                .where(SurfaceModel.tenant_id == tenant_id)
                .order_by(SurfaceModel.created_at.asc())
            )
            return [_row_to_dict(r) for r in result.scalars()]

    async def update(
        self,
        tenant_id: str,
        surface_id: str,
        **fields: Any,
    ) -> dict[str, Any] | None:
        allowed = {
            "name", "surface_type", "persona", "knowledge_allowlist",
            "tool_allowlist", "model_pin", "budgets", "brand_voice_override",
            "active",
        }
        async with self.db.get_session() as session:
            result = await session.execute(
                select(SurfaceModel).where(
                    SurfaceModel.tenant_id == tenant_id,
                    SurfaceModel.id == surface_id,
                )
            )
            row = result.scalar_one_or_none()
            if row is None:
                return None
            for key, value in fields.items():
                if key in allowed:
                    setattr(row, key, value)
            await session.flush()
            return _row_to_dict(row)

    async def delete(self, tenant_id: str, surface_id: str) -> bool:
        async with self.db.get_session() as session:
            result = await session.execute(
                delete(SurfaceModel).where(
                    SurfaceModel.tenant_id == tenant_id,
                    SurfaceModel.id == surface_id,
                )
            )
            return (result.rowcount or 0) > 0


class ToolRegistryRepository:
    """Per-tenant tool registry (Arch 14, P5-3).

    Tools are disabled unless explicitly enabled and authorized for a
    surface. ``enabled_map`` is the authoritative snapshot the tool gate
    reads for a request.
    """

    def __init__(self, db: DatabaseManager):
        self.db = db

    async def list_by_tenant(self, tenant_id: str) -> list[dict[str, Any]]:
        async with self.db.get_session() as session:
            result = await session.execute(
                select(ToolRegistryModel)
                .where(ToolRegistryModel.tenant_id == tenant_id)
                .order_by(ToolRegistryModel.created_at.asc())
            )
            return [_row_to_dict(r) for r in result.scalars()]

    async def get(self, tenant_id: str, name: str) -> dict[str, Any] | None:
        async with self.db.get_session() as session:
            result = await session.execute(
                select(ToolRegistryModel).where(
                    ToolRegistryModel.tenant_id == tenant_id,
                    ToolRegistryModel.name == name,
                )
            )
            row = result.scalar_one_or_none()
            return _row_to_dict(row) if row else None

    async def enabled_map(self, tenant_id: str) -> dict[str, bool]:
        """name -> enabled for every registered tool (authoritative gate)."""
        tools = await self.list_by_tenant(tenant_id)
        return {t["name"]: bool(t["enabled"]) for t in tools}

    async def register(
        self,
        tenant_id: str,
        name: str,
        *,
        description: str | None = None,
        schema: dict[str, Any] | None = None,
        enabled: bool = False,
        auth_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        async with self.db.get_session() as session:
            model = ToolRegistryModel(
                id=str(uuid4()),
                tenant_id=tenant_id,
                name=name,
                description=description,
                schema=schema or {"type": "object"},
                enabled=enabled,
                auth_config=auth_config,
            )
            session.add(model)
            await session.flush()
            return _row_to_dict(model)

    async def set_enabled(
        self, tenant_id: str, name: str, enabled: bool
    ) -> dict[str, Any] | None:
        async with self.db.get_session() as session:
            result = await session.execute(
                select(ToolRegistryModel).where(
                    ToolRegistryModel.tenant_id == tenant_id,
                    ToolRegistryModel.name == name,
                )
            )
            row = result.scalar_one_or_none()
            if row is None:
                return None
            row.enabled = bool(enabled)
            await session.flush()
            return _row_to_dict(row)


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
        """The tenant's active (published) policy set — the one the runtime
        evaluates. Drafts never govern traffic."""
        async with self.db.get_session() as session:
            result = await session.execute(
                select(PolicySetModel)
                .where(
                    PolicySetModel.tenant_id == tenant_id,
                    PolicySetModel.status == "published",
                )
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

    async def list_sets(self, tenant_id: str) -> list[dict[str, Any]]:
        """Every policy set for a tenant (all statuses), newest version first,
        rules included."""
        async with self.db.get_session() as session:
            result = await session.execute(
                select(PolicySetModel)
                .where(PolicySetModel.tenant_id == tenant_id)
                .order_by(PolicySetModel.version.desc())
            )
            sets: list[dict[str, Any]] = []
            for row in result.scalars():
                rules_result = await session.execute(
                    select(PolicyRuleModel)
                    .where(PolicyRuleModel.policy_set_id == row.id)
                    .order_by(PolicyRuleModel.priority.desc())
                )
                data = _row_to_dict(row)
                data["rules"] = [_row_to_dict(r) for r in rules_result.scalars()]
                sets.append(data)
            return sets

    async def get_set(
        self, set_id: str, tenant_id: str
    ) -> dict[str, Any] | None:
        async with self.db.get_session() as session:
            result = await session.execute(
                select(PolicySetModel).where(
                    PolicySetModel.id == set_id,
                    PolicySetModel.tenant_id == tenant_id,
                )
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

    async def update_set_rules(
        self,
        set_id: str,
        tenant_id: str,
        *,
        name: str | None = None,
        rules: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        """Mutate a draft set in place; editing a published revision creates a
        new draft on top (published revisions stay immutable). Returns the
        affected set with rules included."""
        async with self.db.get_session() as session:
            result = await session.execute(
                select(PolicySetModel).where(
                    PolicySetModel.id == set_id,
                    PolicySetModel.tenant_id == tenant_id,
                )
            )
            row = result.scalar_one_or_none()
            if row is None:
                return None
            target = row
            if row.status != "draft":
                max_version = (
                    await session.execute(
                        select(func.max(PolicySetModel.version)).where(
                            PolicySetModel.tenant_id == tenant_id
                        )
                    )
                ).scalar() or 0
                target = PolicySetModel(
                    id=str(uuid4()),
                    tenant_id=tenant_id,
                    name=name or row.name,
                    version=max_version + 1,
                    status="draft",
                )
                session.add(target)
                await session.flush()
            if name is not None:
                target.name = name
            if rules is not None:
                old_rules = (
                    await session.execute(
                        select(PolicyRuleModel).where(
                            PolicyRuleModel.policy_set_id == target.id
                        )
                    )
                ).scalars()
                for old in old_rules:
                    await session.delete(old)
                for rule_data in rules:
                    session.add(
                        PolicyRuleModel(
                            id=str(uuid4()),
                            policy_set_id=target.id,
                            name=rule_data.get("name", ""),
                            policy_type=rule_data.get("policy_type", "topic_filter"),
                            action=rule_data.get("action", "allow"),
                            conditions=rule_data.get("conditions", {}),
                            priority=rule_data.get("priority", 0),
                            enabled=rule_data.get("enabled", True),
                        )
                    )
            await session.flush()
            rules_result = await session.execute(
                select(PolicyRuleModel)
                .where(PolicyRuleModel.policy_set_id == target.id)
                .order_by(PolicyRuleModel.priority.desc())
            )
            data = _row_to_dict(target)
            data["rules"] = [_row_to_dict(r) for r in rules_result.scalars()]
            return data

    async def publish_set(self, set_id: str, tenant_id: str) -> dict[str, Any] | None:
        """Promote a draft to published (idempotent for published sets)."""
        async with self.db.get_session() as session:
            result = await session.execute(
                select(PolicySetModel).where(
                    PolicySetModel.id == set_id,
                    PolicySetModel.tenant_id == tenant_id,
                )
            )
            row = result.scalar_one_or_none()
            if row is None:
                return None
            if row.status != "published":
                row.status = "published"
                await session.flush()
            return _row_to_dict(row)


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
            existing = await session.get(WebhookEventModel, event_id)
            if existing is not None:
                return _row_to_dict(existing)
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
