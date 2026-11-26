"""add batch generation heartbeat

Revision ID: mu_batch_generation_heartbeat
Revises: mu_agent_conversation_title_edited
Create Date: 2026-11-09 10:00:00.000000

批量生成任务心跳列：后台协程在每章开始 / 每次预算重试前刷新。
worker 被重启（uvicorn --reload）杀死后，读取接口据心跳超时把
永久 running 的任务恢复为 failed，避免任务面板永远停在运行中。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "mu_batch_generation_heartbeat"
down_revision: Union[str, None] = "mu_agent_conversation_title_edited"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("batch_generation_tasks", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "heartbeat_at",
                sa.DateTime(),
                nullable=True,
                comment="worker 心跳时间（陈旧任务回收依据）",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("batch_generation_tasks", schema=None) as batch_op:
        batch_op.drop_column("heartbeat_at")
