"""per-tenant model catalog

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-01

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "model_catalog",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("cost_ceiling_per_1k", sa.Float(), nullable=False),
        sa.Column("fallback_order", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "provider", "model", name="uq_model_catalog_tenant_provider_model"
        ),
    )
    op.create_index("ix_model_catalog_tenant", "model_catalog", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_model_catalog_tenant", table_name="model_catalog")
    op.drop_table("model_catalog")
