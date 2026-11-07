"""add world facts provenance

Revision ID: mu_world_facts
Revises: mu_schema_parity
Create Date: 2026-11-07 16:10:00.000000

issue #242（P1）：世界设定由四段自由文本改为「原子事实 + 原文引用」的
可溯源结构。四个 world_* 文本列继续保留（提示词/UI 兼容），这里补上保存
事实本身的列。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "mu_world_facts"
down_revision: Union[str, None] = "mu_schema_parity"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "world_facts",
                sa.Text(),
                nullable=True,
                comment="世界设定事实(JSON claims，含证据与来源)",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.drop_column("world_facts")
