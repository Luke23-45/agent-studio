"""session engine + governance + gateway tables

Adds the durable thread store (Arch 7.1/7.3), the governance-plane tables
(Arch 6.1/6.4/12), and the gateway cost/quota/cache tables (Arch 10),
and extends `messages` with thread-store fields (tenant_id, thread_id, seq,
request_id, parent_message_id, surface_id, end_user_id).

Schema only. Data backfill for existing conversations/messages runs via
`backend/scripts/backfill_threads_0004.py` (idempotent).

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-06

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _json() -> sa.JSON:
    """JSON column with JSONB variant for Postgres, JSON for SQLite."""
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    # ---- session layer (Arch 7.1, 7.3) ----
    op.create_table(
        "threads",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("conversation_id", sa.String(length=36), nullable=True),
        sa.Column("surface_id", sa.String(length=36), nullable=True),
        sa.Column("end_user_id", sa.String(length=36), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("summary_block", _json(), nullable=True),
        sa.Column("summary_position", sa.Integer(), nullable=True),
        sa.Column("summary_version", sa.Integer(), nullable=False),
        sa.Column("forked_from", sa.String(length=36), nullable=True),
        sa.Column("archived", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_threads_tenant_created", "threads", ["tenant_id", "created_at"])
    op.create_index("ix_threads_tenant_end_user", "threads", ["tenant_id", "end_user_id"])

    op.create_table(
        "thread_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("thread_id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("payload", _json(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["thread_id"], ["threads.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("thread_id", "seq", name="uq_thread_events_thread_seq"),
    )
    op.create_index("ix_thread_events_thread_created", "thread_events", ["thread_id", "created_at"])
    op.create_index("ix_thread_events_tenant_created", "thread_events", ["tenant_id", "created_at"])

    op.create_table(
        "message_parts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("message_id", sa.String(length=36), nullable=False),
        sa.Column("thread_id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("part_type", sa.String(length=16), nullable=False),
        sa.Column("part_index", sa.Integer(), nullable=False),
        sa.Column("content", _json(), nullable=False),
        sa.Column("redacted_content", _json(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["thread_id"], ["threads.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("message_id", "part_index", name="uq_message_parts_message_index"),
    )
    op.create_index("ix_message_parts_thread_index", "message_parts", ["thread_id", "part_index"])
    op.create_index("ix_message_parts_tenant_created", "message_parts", ["tenant_id", "created_at"])

    # ---- governance layer (Arch 6.1, 6.4, 12) ----
    op.create_table(
        "surfaces",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("surface_type", sa.String(length=16), nullable=False),
        sa.Column("persona", sa.String(length=512), nullable=True),
        sa.Column("knowledge_allowlist", _json(), nullable=False),
        sa.Column("tool_allowlist", _json(), nullable=False),
        sa.Column("model_pin", sa.String(length=128), nullable=True),
        sa.Column("budgets", _json(), nullable=False),
        sa.Column("brand_voice_override", _json(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_surfaces_tenant", "surfaces", ["tenant_id"])

    op.create_table(
        "end_users",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("external_id", sa.String(length=256), nullable=True),
        sa.Column("anonymous_identity", sa.String(length=256), nullable=True),
        sa.Column("display_name", sa.String(length=256), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "external_id", name="uq_end_users_tenant_external"),
        sa.UniqueConstraint(
            "tenant_id", "anonymous_identity", name="uq_end_users_tenant_anonymous"
        ),
    )
    op.create_index("ix_end_users_tenant", "end_users", ["tenant_id"])

    op.create_table(
        "tenant_config_versions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("config", _json(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("promoted_by", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "version", name="uq_tenant_config_versions_tenant_version"),
    )
    op.create_index(
        "ix_tenant_config_versions_tenant_status", "tenant_config_versions", ["tenant_id", "status"]
    )

    op.create_table(
        "tool_registry",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.String(length=512), nullable=True),
        sa.Column("schema", _json(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("auth_config", _json(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "name", name="uq_tool_registry_tenant_name"),
    )
    op.create_index("ix_tool_registry_tenant", "tool_registry", ["tenant_id"])

    op.create_table(
        "tenant_provider_keys",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("encrypted_key", sa.Text(), nullable=False),
        sa.Column("kms_ref", sa.String(length=256), nullable=True),
        sa.Column("key_source", sa.String(length=16), nullable=False),
        sa.Column("key_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "provider", name="uq_tenant_provider_keys_tenant_provider"),
    )
    op.create_index("ix_tenant_provider_keys_tenant", "tenant_provider_keys", ["tenant_id"])

    # ---- gateway layer (Arch 10) ----
    op.create_table(
        "spend_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("surface_id", sa.String(length=36), nullable=True),
        sa.Column("end_user_id", sa.String(length=36), nullable=True),
        sa.Column("conversation_id", sa.String(length=36), nullable=True),
        sa.Column("session_id", sa.String(length=128), nullable=True),
        sa.Column("provider", sa.String(length=32), nullable=True),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("reasoning_tokens", sa.Integer(), nullable=False),
        sa.Column("cached_tokens", sa.Integer(), nullable=False),
        sa.Column("usd", sa.Float(), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_spend_events_tenant_created", "spend_events", ["tenant_id", "created_at"])

    op.create_table(
        "quota_state",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("scope_type", sa.String(length=16), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=True),
        sa.Column("surface_id", sa.String(length=36), nullable=True),
        sa.Column("end_user_id", sa.String(length=36), nullable=True),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reserved_usd", sa.Float(), nullable=False),
        sa.Column("spent_usd", sa.Float(), nullable=False),
        sa.Column("limit_usd", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "scope_type",
            "tenant_id",
            "surface_id",
            "end_user_id",
            "window_started_at",
            name="uq_quota_state_scope",
        ),
    )
    op.create_index("ix_quota_state_tenant", "quota_state", ["tenant_id"])

    op.create_table(
        "cache_invalidation_log",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("scope", sa.String(length=32), nullable=False),
        sa.Column("resource_id", sa.String(length=64), nullable=True),
        sa.Column("reason", sa.String(length=256), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_cache_invalidation_tenant_created",
        "cache_invalidation_log",
        ["tenant_id", "created_at"],
    )

    # ---- extend messages with thread-store fields (Arch 7.1) ----
    op.add_column("messages", sa.Column("tenant_id", sa.String(length=36), nullable=True))
    op.add_column("messages", sa.Column("thread_id", sa.String(length=36), nullable=True))
    op.add_column("messages", sa.Column("seq", sa.Integer(), nullable=True))
    op.add_column("messages", sa.Column("request_id", sa.String(length=64), nullable=True))
    op.add_column("messages", sa.Column("parent_message_id", sa.String(length=36), nullable=True))
    op.add_column("messages", sa.Column("surface_id", sa.String(length=36), nullable=True))
    op.add_column("messages", sa.Column("end_user_id", sa.String(length=36), nullable=True))
    op.create_unique_constraint("uq_messages_thread_seq", "messages", ["thread_id", "seq"])
    op.create_index("ix_messages_request_id", "messages", ["request_id"], unique=True)
    op.create_index("ix_messages_tenant_created", "messages", ["tenant_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_messages_tenant_created", table_name="messages")
    op.drop_index("ix_messages_request_id", table_name="messages")
    op.drop_constraint("uq_messages_thread_seq", "messages", type_="unique")
    op.drop_column("messages", "end_user_id")
    op.drop_column("messages", "surface_id")
    op.drop_column("messages", "parent_message_id")
    op.drop_column("messages", "request_id")
    op.drop_column("messages", "seq")
    op.drop_column("messages", "thread_id")
    op.drop_column("messages", "tenant_id")

    op.drop_index("ix_cache_invalidation_tenant_created", table_name="cache_invalidation_log")
    op.drop_table("cache_invalidation_log")
    op.drop_index("ix_quota_state_tenant", table_name="quota_state")
    op.drop_table("quota_state")
    op.drop_index("ix_spend_events_tenant_created", table_name="spend_events")
    op.drop_table("spend_events")

    op.drop_index("ix_tenant_provider_keys_tenant", table_name="tenant_provider_keys")
    op.drop_table("tenant_provider_keys")
    op.drop_index("ix_tool_registry_tenant", table_name="tool_registry")
    op.drop_table("tool_registry")
    op.drop_index("ix_tenant_config_versions_tenant_status", table_name="tenant_config_versions")
    op.drop_table("tenant_config_versions")
    op.drop_index("ix_end_users_tenant", table_name="end_users")
    op.drop_table("end_users")
    op.drop_index("ix_surfaces_tenant", table_name="surfaces")
    op.drop_table("surfaces")

    op.drop_index("ix_message_parts_tenant_created", table_name="message_parts")
    op.drop_index("ix_message_parts_thread_index", table_name="message_parts")
    op.drop_table("message_parts")
    op.drop_index("ix_thread_events_tenant_created", table_name="thread_events")
    op.drop_index("ix_thread_events_thread_created", table_name="thread_events")
    op.drop_table("thread_events")
    op.drop_index("ix_threads_tenant_end_user", table_name="threads")
    op.drop_index("ix_threads_tenant_created", table_name="threads")
    op.drop_table("threads")
