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
    op.add_column("event_record", sa.Column("subtype", sa.String(length=32), nullable=True))


def downgrade() -> None:
    op.drop_column("event_record", "subtype")
