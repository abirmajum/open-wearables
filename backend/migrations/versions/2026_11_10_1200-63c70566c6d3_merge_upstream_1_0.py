"""Join the fork history (provider cycles) with upstream 1.0 (series_sample and aggregates).

Revision ID: 63c70566c6d3
Revises: f82e01ac749d, 3f6b9c2d8e41

Both parents descend from c4e7a1d93b52 and touch disjoint tables, so the merge needs no schema change.
"""

from typing import Sequence, Union

revision: str = "63c70566c6d3"
down_revision: Union[str, Sequence[str], None] = ("f82e01ac749d", "3f6b9c2d8e41")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
