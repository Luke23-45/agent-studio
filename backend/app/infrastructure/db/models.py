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
    """A single message within a conversation.

    Thread-store fields (tenant_id, thread_id, seq, request_id,
    parent_message_id, surface_id, end_user_id) are set by the session
    layer (Arch 7.1); content lives in ``message_parts`` (one text part
    per message from migration 0004 onward).
    """

    __tablename__ = "messages"
    __table_args__ = (
        Index("ix_messages_conversation_created", "conversation_id", "created_at"),
        UniqueConstraint("thread_id", "seq", name="uq_messages_thread_seq"),
        Index("ix_messages_request_id", "request_id", unique=True),
        Index("ix_messages_tenant_created", "tenant_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    thread_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    parent_message_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    surface_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    end_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
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


class ThreadModel(Base, TimestampMixin):
    """Durable thread (Arch 7.1): append-only log per tenant + end user.

    ``summary_block`` is the compaction checkpoint (Arch 8.2); both
    ``summary_position`` and ``summary_version`` pin the immutable summary
    block for prompt-cache stability.
    """

    __tablename__ = "threads"
    __table_args__ = (
        Index("ix_threads_tenant_created", "tenant_id", "created_at"),
        Index("ix_threads_tenant_end_user", "tenant_id", "end_user_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    conversation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    surface_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    end_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    summary_block: Mapped[dict | None] = mapped_column(json_column(), nullable=True)
    summary_position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    summary_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    forked_from: Mapped[str | None] = mapped_column(String(36), nullable=True)
    archived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class ThreadEventModel(Base):
    """Append-only event log per thread (Arch 7.1).

    Every mutation (message admitted, part updated, compaction committed,
    message removed) is a durable event with a monotonically increasing
    per-thread ``seq``. ``request_id`` carries the idempotency key.
    """

    __tablename__ = "thread_events"
    __table_args__ = (
        UniqueConstraint("thread_id", "seq", name="uq_thread_events_thread_seq"),
        Index("ix_thread_events_thread_created", "thread_id", "created_at"),
        Index("ix_thread_events_tenant_created", "tenant_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    thread_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("threads.id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload: Mapped[dict] = mapped_column(json_column(), default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class MessagePartModel(Base):
    """A typed part inside a message (Arch 7.1).

    ``part_type``: text | reasoning | tool_use | tool_result | citation |
    compaction | step. The model only ever sees ``redacted_content``
    (Arch 8.1); raw content is reachable only through access-controlled
    review/DSR paths.
    """

    __tablename__ = "message_parts"
    __table_args__ = (
        UniqueConstraint("message_id", "part_index", name="uq_message_parts_message_index"),
        Index("ix_message_parts_thread_index", "thread_id", "part_index"),
        Index("ix_message_parts_tenant_created", "tenant_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    message_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    thread_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("threads.id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    part_type: Mapped[str] = mapped_column(String(16), nullable=False)
    part_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[dict] = mapped_column(json_column(), nullable=False)
    redacted_content: Mapped[dict] = mapped_column(json_column(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class SurfaceModel(Base, TimestampMixin):
    """A surface deployment of the agent (Arch 6.1).

    A tenant runs multiple surfaces (sales assistant, support assistant)
    on one engine; each carries its own persona, knowledge/tool allowlists,
    model pin, budgets, and brand voice override.
    """

    __tablename__ = "surfaces"
    __table_args__ = (Index("ix_surfaces_tenant", "tenant_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    surface_type: Mapped[str] = mapped_column(String(16), default="widget", nullable=False)
    persona: Mapped[str | None] = mapped_column(String(512), nullable=True)
    knowledge_allowlist: Mapped[list] = mapped_column(json_column(), default=list, nullable=False)
    tool_allowlist: Mapped[list] = mapped_column(json_column(), default=list, nullable=False)
    model_pin: Mapped[str | None] = mapped_column(String(128), nullable=True)
    budgets: Mapped[dict] = mapped_column(json_column(), default=dict, nullable=False)
    brand_voice_override: Mapped[dict | None] = mapped_column(json_column(), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class EndUserModel(Base, TimestampMixin):
    """Per-tenant end user (Arch 6.1, 6.4). No global user table.

    Anonymous end users are keyed by ``anonymous_identity`` (per-device
    identity); authenticated end users by the tenant-issued ``external_id``.
    Identity never crosses tenant boundaries.
    """

    __tablename__ = "end_users"
    __table_args__ = (
        UniqueConstraint("tenant_id", "external_id", name="uq_end_users_tenant_external"),
        UniqueConstraint(
            "tenant_id", "anonymous_identity", name="uq_end_users_tenant_anonymous"
        ),
        Index("ix_end_users_tenant", "tenant_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    anonymous_identity: Mapped[str | None] = mapped_column(String(256), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)


class SessionTokenModel(Base):
    """Durable registry for end-user session tokens (Arch 6.4, P1-8).

    One row per minted token (jti). The bearer string is a separate
    encrypted payload; the row is the revocation source of truth —
    resolution checks ``revoked_at``/``expires_at`` on every request.
    Tokens are scoped to exactly one tenant.
    """

    __tablename__ = "session_tokens"
    __table_args__ = (
        Index("ix_session_tokens_tenant_end_user", "tenant_id", "end_user_id"),
        Index("ix_session_tokens_expires_at", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # jti
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    end_user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("end_users.id", ondelete="CASCADE"), nullable=False
    )
    surface_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    device_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    scopes: Mapped[list] = mapped_column(json_column(), default=list, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class TenantConfigVersionModel(Base):
    """Immutable, versioned tenant config (Arch 12, P0-11).

    The runtime reads the published version; publish/promote/rollback
    writes a new immutable row.
    """

    __tablename__ = "tenant_config_versions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "version", name="uq_tenant_config_versions_tenant_version"),
        Index("ix_tenant_config_versions_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    config: Mapped[dict] = mapped_column(json_column(), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="draft", nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    promoted_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class ToolRegistryModel(Base, TimestampMixin):
    """Per-tenant tool registry (Arch 14 tool gate, P5-6).

    ``schema`` is the JSON tool schema; tools are disabled unless
    explicitly enabled and authorized for a surface.
    """

    __tablename__ = "tool_registry"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_tool_registry_tenant_name"),
        Index("ix_tool_registry_tenant", "tenant_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(String(512), nullable=True)
    schema: Mapped[dict] = mapped_column(json_column(), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    auth_config: Mapped[dict | None] = mapped_column(json_column(), nullable=True)


class TenantProviderKeyModel(Base, TimestampMixin):
    """Per-tenant provider credentials (Arch 6.3.9, P0-8).

    ``encrypted_key`` is the envelope-encrypted secret; ``key_source`` is
    platform-managed | tenant-owned (BYOK). No global key path exists.
    """

    __tablename__ = "tenant_provider_keys"
    __table_args__ = (
        UniqueConstraint("tenant_id", "provider", name="uq_tenant_provider_keys_tenant_provider"),
        Index("ix_tenant_provider_keys_tenant", "tenant_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    encrypted_key: Mapped[str] = mapped_column(Text, nullable=False)
    kms_ref: Mapped[str | None] = mapped_column(String(256), nullable=True)
    key_source: Mapped[str] = mapped_column(String(16), default="platform-managed", nullable=False)
    key_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class SpendEventModel(Base):
    """Append-only spend event (Arch 10 cost ledger, P0-10).

    Every completed turn writes one row (tenant, surface, end-user, model,
    provider, tokens, USD); consumers aggregate for billing and dashboards.
    """

    __tablename__ = "spend_events"
    __table_args__ = (Index("ix_spend_events_tenant_created", "tenant_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    surface_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    end_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    conversation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reasoning_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cached_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class QuotaStateModel(Base, TimestampMixin):
    """USD quota reservation/reconciliation state (Arch 10, P3-6).

    One row per (scope_type, tenant, surface, end_user, window).
    """

    __tablename__ = "quota_state"
    __table_args__ = (
        UniqueConstraint(
            "scope_type",
            "tenant_id",
            "surface_id",
            "end_user_id",
            "window_started_at",
            name="uq_quota_state_scope",
        ),
        Index("ix_quota_state_tenant", "tenant_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    scope_type: Mapped[str] = mapped_column(String(16), nullable=False)
    tenant_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    surface_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    end_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    window_started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    reserved_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    spent_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    limit_usd: Mapped[float | None] = mapped_column(Float, nullable=True)


class CacheInvalidationLogModel(Base):
    """Durable record of cache invalidations (Arch 10, P3-7)."""

    __tablename__ = "cache_invalidation_log"
    __table_args__ = (
        Index("ix_cache_invalidation_tenant_created", "tenant_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    scope: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
