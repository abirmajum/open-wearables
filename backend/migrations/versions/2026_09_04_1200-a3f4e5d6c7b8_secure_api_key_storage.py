from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a3f4e5d6c7b8"
down_revision: Union[str, None] = "dc5ac28c4b94"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.add_column("api_key", sa.Column("new_id", sa.UUID(), nullable=True))
    op.add_column("api_key", sa.Column("key_hash", sa.String(length=64), nullable=True))
    op.add_column("api_key", sa.Column("display_prefix", sa.String(length=12), nullable=True))

    op.execute(
        "UPDATE api_key SET "
        "new_id = gen_random_uuid(), "
        "key_hash = encode(digest(convert_to(id, 'UTF8'), 'sha256'), 'hex'), "
        "display_prefix = left(id, 11)"
    )

    op.alter_column("api_key", "new_id", existing_type=sa.UUID(), nullable=False)
    op.alter_column("api_key", "key_hash", existing_type=sa.String(length=64), nullable=False)
    op.alter_column("api_key", "display_prefix", existing_type=sa.String(length=12), nullable=False)
    op.drop_constraint("api_key_pkey", "api_key", type_="primary")
    op.drop_column("api_key", "id")
    op.alter_column(
        "api_key",
        "new_id",
        new_column_name="id",
        existing_type=sa.UUID(),
        existing_nullable=False,
    )
    op.create_primary_key("api_key_pkey", "api_key", ["id"])
    op.create_index("ix_api_key_key_hash", "api_key", ["key_hash"], unique=True)


def downgrade() -> None:
    raise RuntimeError("API key plaintext was intentionally discarded; this migration cannot be downgraded safely.")
