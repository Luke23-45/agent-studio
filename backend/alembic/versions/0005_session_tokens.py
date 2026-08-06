"""session tokens

Adds the durable session-token registry (Arch 6.4, P1-8): each minted
token is a jti row bound to exactly one tenant + end user (+ optional
surface/device/scopes), with expiry and revocability. The token string
itself is an encrypted payload; the DB row is the revocation source of
truth and survives Redis flushes.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-06

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _json() -> sa.JSON:
    """JSON column with JSONB variant for Postgres, JSON for SQLite."""
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "session_tokens",
        sa.Column("id", sa.String(length=64), nullable=False),  # jti
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("end_user_id", sa.String(length=36), nullable=False),
        sa.Column("surface_id", sa.String(length=36), nullable=True),
        sa.Column("device_id", sa.String(length=128), nullable=True),
        sa.Column("scopes", _json(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["end_user_id"], ["end_users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_session_tokens_tenant_end_user",
        "session_tokens",
        ["tenant_id", "end_user_id"],
    )
    op.create_index("ix_session_tokens_expires_at", "session_tokens", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_session_tokens_expires_at", table_name="session_tokens")
    op.drop_index("ix_session_tokens_tenant_end_user", table_name="session_tokens")
    op.drop_table("session_tokens")
