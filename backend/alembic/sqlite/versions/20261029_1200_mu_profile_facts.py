"""add profile facts provenance

Revision ID: mu_profile_facts
Revises: mu_world_profile_sync_state
Create Date: 2026-10-29 12:00:00.000000

issue #190：档案文本列（personality/background/appearance）改为「原子事实 +
原文引用」的渲染产物，这里补上保存事实本身的溯源列。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "mu_profile_facts"
down_revision: Union[str, None] = "mu_world_profile_sync_state"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("characters", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "profile_facts",
                sa.Text(),
                nullable=True,
                comment="最近一次通过校验的档案事实(JSON 数组)",
            )
        )
        batch_op.add_column(
            sa.Column(
                "profile_facts_updated_at",
                sa.DateTime(),
                nullable=True,
                comment="档案事实最后写入时间",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("characters", schema=None) as batch_op:
        batch_op.drop_column("profile_facts_updated_at")
        batch_op.drop_column("profile_facts")
