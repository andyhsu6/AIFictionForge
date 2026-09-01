"""add aliases column to characters

Revision ID: mu_add_character_aliases
Revises: mu_relationship_multi_type
Create Date: 2026-08-31 00:01:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "mu_add_character_aliases"
down_revision: Union[str, None] = "mu_relationship_multi_type"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("characters", schema=None) as batch_op:
        batch_op.add_column(sa.Column("aliases", sa.String(length=500), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("characters", schema=None) as batch_op:
        batch_op.drop_column("aliases")
