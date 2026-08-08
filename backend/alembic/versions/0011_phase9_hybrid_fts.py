"""phase 9: hybrid retrieval -- Postgres FTS tsvector column + GIN index

P9-2 (feature-matrix 7.2/7.3): the durable lexical channel for hybrid
retrieval. Adds a generated ``content_tsv`` column over ``embeddings``
content and a GIN index for ``to_tsquery`` lookups.

Postgres-only: the generated column uses ``to_tsvector``, which SQLite
does not support. The in-memory BM25 index (hybrid.py) serves dev/tests;
this migration only exists for Postgres deployments.

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-07

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute(
        "ALTER TABLE embeddings "
        "ADD COLUMN IF NOT EXISTS content_tsv tsvector "
        "GENERATED ALWAYS AS (to_tsvector('english', content)) STORED"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_embeddings_content_tsv_gin "
        "ON embeddings USING gin (content_tsv)"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute("DROP INDEX IF EXISTS ix_embeddings_content_tsv_gin")
    op.execute("ALTER TABLE embeddings DROP COLUMN IF EXISTS content_tsv")
