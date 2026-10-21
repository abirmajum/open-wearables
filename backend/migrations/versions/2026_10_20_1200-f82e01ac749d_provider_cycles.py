"""Merge the upstream and fork histories and retain physiological cycle revisions."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "f82e01ac749d"
down_revision = ("c4e7a1d93b52", "a3f4e5d6c7b8")
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "provider_cycle",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.UUID(), sa.ForeignKey("user.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("provider", sa.String(64), primary_key=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_provider_cycle_user_updated", "provider_cycle", ["user_id", "updated_at", "id"])


def downgrade() -> None:
    op.drop_table("provider_cycle")
