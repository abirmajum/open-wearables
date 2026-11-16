"""event record subtype

Revision ID: 7d2e4b9a1c53
Revises: 63c70566c6d3

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7d2e4b9a1c53"
down_revision: Union[str, None] = "63c70566c6d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ADD/DROP COLUMN takes an ACCESS EXCLUSIVE lock on event_record: behind a long transaction the migration fails
    # fast and the next start retries, instead of queueing every query behind it. SET LOCAL lasts for its transaction.
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("event_record", sa.Column("subtype", sa.String(length=32), nullable=True))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("event_record", "subtype")
