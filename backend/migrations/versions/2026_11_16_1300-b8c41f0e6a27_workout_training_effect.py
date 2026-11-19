"""workout training effect

Revision ID: b8c41f0e6a27
Revises: 7d2e4b9a1c53

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b8c41f0e6a27"
down_revision: Union[str, None] = "7d2e4b9a1c53"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ADD/DROP COLUMN takes an ACCESS EXCLUSIVE lock on workout_details: behind a long transaction the migration fails
    # fast and the next start retries, instead of queueing every query behind it. SET LOCAL lasts for its transaction.
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("workout_details", sa.Column("aerobic_training_effect", sa.Numeric(5, 2), nullable=True))
    op.add_column("workout_details", sa.Column("anaerobic_training_effect", sa.Numeric(5, 2), nullable=True))
    op.add_column("workout_details", sa.Column("training_load", sa.Numeric(10, 3), nullable=True))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("workout_details", "training_load")
    op.drop_column("workout_details", "anaerobic_training_effect")
    op.drop_column("workout_details", "aerobic_training_effect")
