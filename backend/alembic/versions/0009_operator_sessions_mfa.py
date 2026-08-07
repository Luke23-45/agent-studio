"""operator sessions + per-key MFA (TOTP)

P7-4 schema additions:

- ``operator_sessions``: privileged sessions minted by OIDC SSO
  (authorization-code flow) or MFA-verified logins. Only the SHA-256
  hash of the bearer token is stored; resolution checks ``revoked_at``
  and ``expires_at`` on every request (same contract as session tokens).
- ``api_keys.mfa_secret`` / ``api_keys.mfa_enabled``: per-key TOTP
  enrollment. While enrolling, ``mfa_secret`` holds the pending secret
  and ``mfa_enabled`` stays False; the confirm step flips the flag,
  making the secret active. Secrets are stored encrypted (Fernet
  envelope, KMS-ready — see ``infrastructure/keys/crypto.py``).

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-07

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "operator_sessions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=True),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("idp_sub", sa.String(length=128), nullable=True),
        sa.Column("auth_method", sa.String(length=16), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_operator_sessions_token_hash",
        "operator_sessions",
        ["token_hash"],
        unique=True,
    )
    op.create_index(
        "ix_operator_sessions_expires_at",
        "operator_sessions",
        ["expires_at"],
        unique=False,
    )
    op.add_column(
        "api_keys",
        sa.Column("mfa_secret", sa.Text(), nullable=True),
    )
    op.add_column(
        "api_keys",
        sa.Column("mfa_enabled", sa.Boolean(), nullable=False, server_default=sa.text("0")),
    )


def downgrade() -> None:
    op.drop_column("api_keys", "mfa_enabled")
    op.drop_column("api_keys", "mfa_secret")
    op.drop_index("ix_operator_sessions_expires_at", table_name="operator_sessions")
    op.drop_index("ix_operator_sessions_token_hash", table_name="operator_sessions")
    op.drop_table("operator_sessions")
