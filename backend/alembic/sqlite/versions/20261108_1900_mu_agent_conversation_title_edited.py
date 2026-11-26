"""add agent conversation title_user_edited

Revision ID: mu_agent_conversation_title_edited
Revises: mu_batch_style_check
Create Date: 2026-11-08 12:00:00.000000

issue #262：用户重命名后的会话标题不允许被自动标题生成覆盖。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "mu_agent_conversation_title_edited"
down_revision: Union[str, None] = "mu_batch_style_check"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("agent_conversations", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "title_user_edited",
                sa.Boolean(),
                nullable=False,
                server_default="0",
                comment="会话标题是否被用户显式重命名（issue #262）",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("agent_conversations", schema=None) as batch_op:
        batch_op.drop_column("title_user_edited")
