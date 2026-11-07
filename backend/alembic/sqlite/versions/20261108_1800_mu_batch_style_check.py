"""add batch generation style check snapshot

Revision ID: mu_batch_style_check
Revises: mu_style_profile_p1
Create Date: 2026-11-08 18:00:00.000000

issue #254 P2 补齐：批量生成任务保存最近一章的文笔一致性自检结果，
供状态轮询接口返回给前端提示（只读展示，不回写章节或画像）。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "mu_batch_style_check"
down_revision: Union[str, None] = "mu_style_profile_p1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("batch_generation_tasks", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "style_check",
                sa.JSON(),
                nullable=True,
                comment="最近一章的文笔一致性自检结果(JSON，issue #254)",
            )
        )
        batch_op.add_column(
            sa.Column(
                "anchor_settings",
                sa.JSON(),
                nullable=True,
                comment="本批次锚点覆盖设置(JSON，issue #254)",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("batch_generation_tasks", schema=None) as batch_op:
        batch_op.drop_column("anchor_settings")
        batch_op.drop_column("style_check")
