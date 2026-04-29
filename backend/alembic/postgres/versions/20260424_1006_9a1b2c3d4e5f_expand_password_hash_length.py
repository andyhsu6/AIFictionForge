"""expand password hash length

Revision ID: 9a1b2c3d4e5f
Revises: 6eb27fce64de
Create Date: 2026-04-24 10:06:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9a1b2c3d4e5f'
down_revision: Union[str, None] = '6eb27fce64de'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        'user_passwords',
        'password_hash',
        existing_type=sa.String(length=64),
        type_=sa.String(length=255),
        existing_nullable=False,
        existing_comment='密码哈希（SHA256）',
        comment='密码哈希',
    )


def downgrade() -> None:
    op.alter_column(
        'user_passwords',
        'password_hash',
        existing_type=sa.String(length=255),
        type_=sa.String(length=64),
        existing_nullable=False,
        existing_comment='密码哈希',
        comment='密码哈希（SHA256）',
    )
