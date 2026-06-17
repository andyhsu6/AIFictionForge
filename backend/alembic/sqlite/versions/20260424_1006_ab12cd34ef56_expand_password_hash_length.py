"""expand password hash length

Revision ID: ab12cd34ef56
Revises: 6ff45db05863
Create Date: 2026-04-24 10:06:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ab12cd34ef56'
down_revision: Union[str, None] = '6ff45db05863'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('user_passwords', schema=None) as batch_op:
        batch_op.alter_column(
            'password_hash',
            existing_type=sa.String(length=64),
            type_=sa.String(length=255),
            existing_nullable=False,
        )


def downgrade() -> None:
    with op.batch_alter_table('user_passwords', schema=None) as batch_op:
        batch_op.alter_column(
            'password_hash',
            existing_type=sa.String(length=255),
            type_=sa.String(length=64),
            existing_nullable=False,
        )
