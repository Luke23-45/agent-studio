"""phase 9: versioned tenant prompts (P9-4 prompt management portal)

Adds the ``prompts`` table: one row per (tenant, name, version) with
``enabled`` + ``target_percentage`` for deterministic A/B traffic splits.
New versions start disabled; the conversation path resolves the active
variant of ``system`` per session seed.

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-07

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS prompts (
            id VARCHAR(36) NOT NULL,
            tenant_id VARCHAR(36) NOT NULL,
            name VARCHAR(128) NOT NULL,
            version INTEGER NOT NULL,
            content TEXT NOT NULL,
            description VARCHAR(512),
            enabled BOOLEAN NOT NULL DEFAULT FALSE,
            target_percentage INTEGER NOT NULL DEFAULT 0,
            created_by VARCHAR(128),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (id),
            CONSTRAINT uq_prompts_tenant_name_version UNIQUE (tenant_id, name, version)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_prompts_tenant_name "
        "ON prompts (tenant_id, name)"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute("DROP TABLE IF EXISTS prompts")
