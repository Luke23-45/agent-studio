"""phase 8: schema discipline -- tenant-leading composite indexes

Phase 8.2 audit follow-up (docs/implementation/schema_discipline.md):

- ``messages (tenant_id, thread_id, seq)``: thread-scoped reads also filter
  ``tenant_id``; the tenant-leading composite serves them directly and
  enables partition pruning under the P8-4 tenant-hash sharding design.
- ``thread_events (tenant_id, thread_id, seq)``: same rationale for the
  per-thread event log.

Both indexes are additive only (CREATE INDEX); existing queries keep their
previous plans.

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-07

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_messages_tenant_thread_seq",
        "messages",
        ["tenant_id", "thread_id", "seq"],
    )
    op.create_index(
        "ix_thread_events_tenant_thread_seq",
        "thread_events",
        ["tenant_id", "thread_id", "seq"],
    )


def downgrade() -> None:
    op.drop_index("ix_thread_events_tenant_thread_seq", table_name="thread_events")
    op.drop_index("ix_messages_tenant_thread_seq", table_name="messages")
