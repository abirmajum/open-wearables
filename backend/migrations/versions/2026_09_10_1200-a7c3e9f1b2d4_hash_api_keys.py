"""hash_api_keys

Store API keys as SHA-256 hashes with a display prefix instead of the raw value,
and switch the primary key from the raw key string to a UUID.

Revision ID: a7c3e9f1b2d4
Revises: cf76dead11f5

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7c3e9f1b2d4"
down_revision: Union[str, None] = "cf76dead11f5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = "a3f4e5d6c7b8"


def upgrade() -> None:
    # This fork already discarded plaintext keys in a3f4e5d6c7b8. Preserve
    # both their opaque IDs and hashes; only adapt the display-column name.
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("api_key")}
    if "key_hash" in columns:
        if "display_prefix" in columns and "key_prefix" not in columns:
            op.alter_column("api_key", "display_prefix", new_column_name="key_prefix", type_=sa.String(32))
        return
    op.add_column("api_key", sa.Column("uuid_id", sa.UUID(), nullable=True))
    op.add_column("api_key", sa.Column("key_hash", sa.String(length=64), nullable=True))
    op.add_column("api_key", sa.Column("key_prefix", sa.String(length=32), nullable=True))

    # Existing rows hold the raw key in "id": hash it in place so no key stops working.
    op.execute(
        """
        UPDATE api_key
        SET uuid_id = gen_random_uuid(),
            key_hash = encode(sha256(convert_to(id, 'UTF8')), 'hex'),
            key_prefix = left(id, 10)
        """
    )

    op.alter_column("api_key", "uuid_id", nullable=False)
    op.alter_column("api_key", "key_hash", nullable=False)
    op.alter_column("api_key", "key_prefix", nullable=False)

    op.drop_constraint("api_key_pkey", "api_key", type_="primary")
    op.drop_column("api_key", "id")
    op.alter_column("api_key", "uuid_id", new_column_name="id")
    op.create_primary_key("api_key_pkey", "api_key", ["id"])
    op.create_unique_constraint("api_key_key_hash_key", "api_key", ["key_hash"])


def downgrade() -> None:
    raise RuntimeError("Hashed API keys cannot be safely downgraded; restore a reviewed database backup.")
