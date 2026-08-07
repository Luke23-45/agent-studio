"""governance: region, config validation, Postgres RLS

Phase 5 (governance plane) schema additions:
- ``tenants.region`` (P5-12 residency pinning).
- ``tenant_config_versions`` validation-status columns (P5-2 approvals /
  eval-gated promotion: a version is draft until validated, then approved,
  then published; default ``""`` keeps existing rows promotable).
- Postgres Row-Level Security (P5-6): enable RLS + one policy per
  tenant-scoped table, gated on the Postgres dialect. SQLite (dev/test)
  is untouched — RLS has no meaning there.

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-07

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TENANT_SCOPED_TABLES = (
    "tenants",
    "conversations",
    "messages",
    "message_parts",
    "threads",
    "thread_events",
    "guardrail_evidence",
    "audit_events",
    "api_keys",
    "escalations",
    "policy_sets",
    "policy_rules",
    "webhook_subscriptions",
    "webhook_events",
    "webhook_deliveries",
    "event_outbox",
    "model_catalog",
    "memories",
    "surfaces",
    "end_users",
    "session_tokens",
    "tenant_config_versions",
    "tool_registry",
    "tenant_provider_keys",
    "spend_events",
    "quota_state",
    "cache_invalidation_log",
)


def _tenant_scope_expr(table: str) -> str:
    """Predicate scoping ``table`` to the ``app.tenant_id`` GUC.

    Mirrors ``governance.rls.tenant_scope_expr`` (kept inline so the
    migration has no app-code dependency): ``tenants`` scopes by ``id``,
    ``policy_rules`` is reached through ``policy_sets``, everything else by
    ``tenant_id``.
    """
    if table == "tenants":
        return "id = current_setting('app.tenant_id', true)"
    if table == "policy_rules":
        return (
            "policy_set_id IN (SELECT id FROM policy_sets "
            "WHERE tenant_id = current_setting('app.tenant_id', true))"
        )
    return "tenant_id = current_setting('app.tenant_id', true)"


def _enable_rls(table: str) -> None:
    scope = _tenant_scope_expr(table)
    op.execute(text(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY;'))
    op.execute(
        text(
            f'DROP POLICY IF EXISTS "tenant_isolation_{table}" ON "{table}";'
        )
    )
    op.execute(
        text(
            f'CREATE POLICY "tenant_isolation_{table}" ON "{table}" '
            f"USING ("
            f"current_setting('app.tenant_id', true) IS NULL "
            f"OR {scope}) "
            f"WITH CHECK ("
            f"current_setting('app.tenant_id', true) IS NULL "
            f"OR {scope});"
        )
    )


def upgrade() -> None:
    op.add_column("tenants", sa.Column("region", sa.String(length=32), nullable=True))

    op.add_column(
        "tenant_config_versions",
        sa.Column("validation_status", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "tenant_config_versions",
        sa.Column("validated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "tenant_config_versions",
        sa.Column("approved_by", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "tenant_config_versions",
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
    )

    if op.get_bind().dialect.name == "postgresql":
        # Defense-in-depth beneath tenant-scoped repositories (P5-6).
        for table in TENANT_SCOPED_TABLES:
            _enable_rls(table)


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for table in reversed(TENANT_SCOPED_TABLES):
            op.execute(
                text(f'DROP POLICY IF EXISTS "tenant_isolation_{table}" ON "{table}";')
            )
            op.execute(text(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY;'))

    op.drop_column("tenant_config_versions", "approved_at")
    op.drop_column("tenant_config_versions", "approved_by")
    op.drop_column("tenant_config_versions", "validated_at")
    op.drop_column("tenant_config_versions", "validation_status")
    op.drop_column("tenants", "region")