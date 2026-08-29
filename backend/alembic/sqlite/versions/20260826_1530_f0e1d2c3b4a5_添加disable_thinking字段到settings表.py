"""添加disable_thinking字段到settings表

Revision ID: f0e1d2c3b4a5
Revises: f1a2b3c4d5e6
Create Date: 2026-08-26 15:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f0e1d2c3b4a5'
down_revision: Union[str, None] = 'f1a2b3c4d5e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('settings', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'disable_thinking',
                sa.Boolean(),
                server_default='0',
                nullable=False,
                comment='是否关闭模型思考（开启后思考型模型跳过思考阶段直接输出正文）',
            )
        )


def downgrade() -> None:
    with op.batch_alter_table('settings', schema=None) as batch_op:
        batch_op.drop_column('disable_thinking')
