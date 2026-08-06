"""
SQLAlchemy ORM models for Neryva Agent Studio persistence.

UUIDs are stored as String(36) so the same schema works on SQLite (dev)
and Postgres (prod). JSON columns use JSONB on Postgres for queryability.
"""

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Index,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


def json_column() -> JSON:
    """JSON column type with JSONB variant for Postgres."""
    return JSON().with_variant(JSONB(), "postgresql")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class TenantModel(Base, TimestampMixin):
    """Persisted tenant configuration. Replaces JSON-file storage."""

    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    slug: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(256), nullable=False)

    allowed_topics: Mapped[list] = mapped_column(json_column(), default=list, nullable=False)
    blocked_topics: Mapped[list] = mapped_column(json_column(), default=list, nullable=False)
    escalation_threshold: Mapped[float] = mapped_column(Float, default=0.7, nullable=False)
    knowledge_allowlist: Mapped[list] = mapped_column(json_column(), default=list, nullable=False)

    default_provider: Mapped[str] = mapped_column(String(32), default="openai", nullable=False)
    default_model: Mapped[str] = mapped_column(String(128), default="gpt-4", nullable=False)

    features: Mapped[dict] = mapped_column(json_column(), default=dict, nullable=False)
    guardrail_config: Mapped[dict] = mapped_column(json_column(), default=dict, nullable=False)
    guardrail_thresholds: Mapped[dict] = mapped_column(json_column(), default=dict, nullable=False)

    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class ConversationModel(Base, TimestampMixin):
    """A conversation session, identified by tenant + session_id."""

    __tablename__ = "conversations"
    __table_args__ = (UniqueConstraint("tenant_id", "session_id", name="uq_conversation_session"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    session_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    is_escalated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class MessageModel(Base):
    """A single message within a conversation."""

    __tablename__ = "messages"
    __table_args__ = (Index("ix_messages_conversation_created", "conversation_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    redacted_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict] = mapped_column("metadata", json_column(), default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class GuardrailEvidenceModel(Base):
    """Immutable record of every guardrail decision (audit evidence packet)."""

    __tablename__ = "guardrail_evidence"
    __table_args__ = (Index("ix_evidence_tenant_created", "tenant_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    conversation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)  # input | output
    decision: Mapped[str] = mapped_column(String(16), nullable=False)  # ALLOW | BLOCK | ...
    allowed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    violations: Mapped[list] = mapped_column(json_column(), default=list, nullable=False)
    layers_evaluated: Mapped[list] = mapped_column(json_column(), default=list, nullable=False)
    processing_time_ms: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    metadata_json: Mapped[dict] = mapped_column("metadata", json_column(), default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class AuditEventModel(Base):
    """Immutable admin audit trail."""

    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_tenant_created", "tenant_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    actor_type: Mapped[str] = mapped_column(String(16), nullable=False)  # api_key | system
    actor_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    details: Mapped[dict] = mapped_column(json_column(), default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class ApiKeyModel(Base, TimestampMixin):
    """API key record. Only the SHA-256 hash of the key is stored."""

    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    prefix: Mapped[str] = mapped_column(String(32), nullable=False)
    role: Mapped[str] = mapped_column(String(32), default="operator", nullable=False)
    tenant_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    scopes: Mapped[list] = mapped_column(json_column(), default=list, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    usage_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class EscalationModel(Base, TimestampMixin):
    """Persisted human-handoff / escalation record."""

    __tablename__ = "escalations"
    __table_args__ = (Index("ix_escalations_tenant_status", "tenant_id", "status"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    conversation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    category: Mapped[str] = mapped_column(String(32), default="general", nullable=False)
    severity: Mapped[str] = mapped_column(String(16), default="medium", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="open", nullable=False)
    reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    details: Mapped[dict] = mapped_column(json_column(), default=dict, nullable=False)
    channel: Mapped[str] = mapped_column(String(32), default="generic", nullable=False)
    external_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    sla_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PolicySetModel(Base, TimestampMixin):
    """Versioned policy set attached to a tenant."""

    __tablename__ = "policy_sets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="published", nullable=False)


class PolicyRuleModel(Base, TimestampMixin):
    """A single rule inside a policy set."""

    __tablename__ = "policy_rules"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    policy_set_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("policy_sets.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    policy_type: Mapped[str] = mapped_column(String(32), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    conditions: Mapped[dict] = mapped_column(json_column(), default=dict, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class WebhookSubscriptionModel(Base, TimestampMixin):
    """Customer-registered webhook subscription (matrix 1.7).

    ``secret`` is the HMAC signing secret for this subscription; in
    production it should come from a secret manager (KMS-ready).
    """

    __tablename__ = "webhook_subscriptions"
    __table_args__ = (Index("ix_webhook_subs_tenant", "tenant_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    url: Mapped[str] = mapped_column(String(1024), nullable=False)
    secret: Mapped[str] = mapped_column(String(256), nullable=False)
    events: Mapped[list] = mapped_column(json_column(), default=list, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class WebhookEventModel(Base, TimestampMixin):
    """Every published webhook event; the durable source for replay."""

    __tablename__ = "webhook_events"
    __table_args__ = (Index("ix_webhook_events_tenant", "tenant_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(json_column(), default=dict, nullable=False)
    delivered_to: Mapped[list] = mapped_column(json_column(), default=list, nullable=False)


class WebhookDeliveryModel(Base, TimestampMixin):
    """One delivery attempt (or final outcome) per event/subscription."""

    __tablename__ = "webhook_deliveries"
    __table_args__ = (Index("ix_webhook_deliveries_event", "event_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(36), nullable=False)
    subscription_id: Mapped[str] = mapped_column(String(36), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # success | failed | pending
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(String(512), nullable=True)


class ModelCatalogModel(Base, TimestampMixin):
    """Per-tenant model catalog entry (matrix 5.2).

    Controls which (provider, model) pairs a tenant may use, their fallback
    order, and a per-1k-tokens cost ceiling. ``cost_ceiling_per_1k`` = 0
    means unlimited.
    """

    __tablename__ = "model_catalog"
    __table_args__ = (
        UniqueConstraint("tenant_id", "provider", "model", name="uq_model_catalog_tenant_provider_model"),
        Index("ix_model_catalog_tenant", "tenant_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    cost_ceiling_per_1k: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    fallback_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
