"""governance: config promotion gates (canary + eval suite)

Phase 5.2 schema additions to ``tenant_config_versions``:

- ``canary_percent`` (0-100): a published version with a sub-100 value is a
  canary rollout — the runtime serves it to a deterministic slice of request
  keys and keeps the previously published version live as the baseline.
- ``eval_status`` / ``eval_details``: the eval-suite gate (P6-6 slot) so a
  version whose suite failed cannot be promoted.

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-07

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tenant_config_versions",
        sa.Column("canary_percent", sa.Integer(), nullable=True),
    )
    op.add_column(
        "tenant_config_versions",
        sa.Column("eval_status", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "tenant_config_versions",
        sa.Column("eval_details", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tenant_config_versions", "eval_details")
    op.drop_column("tenant_config_versions", "eval_status")
    op.drop_column("tenant_config_versions", "canary_percent")