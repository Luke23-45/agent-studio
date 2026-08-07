"""governance: immutable audit chain + per-tenant retention

Phase 5.9 schema additions:

- ``audit_events.prev_hash`` / ``audit_events.event_hash``: the tamper-evident
  SHA-256 chain (P5-9). Rows predating this migration keep NULL hashes and are
  treated as unverifiable legacy entries by ``verify_chain``.
- ``audit_events`` is enforced append-only on Postgres via a trigger that
  rejects UPDATE/DELETE (the application already exposes no mutation surface).
- ``tenants.retention_days``: per-tenant retention window (P6-8 slot).

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-07

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "audit_events",
        sa.Column("prev_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "audit_events",
        sa.Column("event_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "tenants",
        sa.Column("retention_days", sa.Integer(), nullable=True),
    )
    _install_append_only_trigger()


def downgrade() -> None:
    _uninstall_append_only_trigger()
    op.drop_column("tenants", "retention_days")
    op.drop_column("audit_events", "event_hash")
    op.drop_column("audit_events", "prev_hash")


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _install_append_only_trigger() -> None:
    if not _is_postgres():
        return
    op.execute(
        """
        CREATE FUNCTION block_audit_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'audit_events is append-only';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_events_append_only
        BEFORE UPDATE OR DELETE ON audit_events
        FOR EACH ROW EXECUTE FUNCTION block_audit_mutation()
        """
    )


def _uninstall_append_only_trigger() -> None:
    if not _is_postgres():
        return
    op.execute("DROP TRIGGER IF EXISTS audit_events_append_only ON audit_events")
    op.execute("DROP FUNCTION IF EXISTS block_audit_mutation()")
