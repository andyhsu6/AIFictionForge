"""add world profile sync state

Revision ID: mu_world_profile_sync_state
Revises: mu_profile_sync_state
Create Date: 2026-10-28 14:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "mu_world_profile_sync_state"
down_revision: Union[str, None] = "mu_profile_sync_state"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "world_profile_synced_through_chapter",
                sa.Integer(),
                nullable=False,
                server_default="0",
                comment="世界设定已同步到的章节号",
            )
        )
        batch_op.add_column(
            sa.Column(
                "ai_world_profile_snapshot",
                sa.Text(),
                nullable=True,
                comment="上一次 AI 写入的世界设定快照(JSON)",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.drop_column("ai_world_profile_snapshot")
        batch_op.drop_column("world_profile_synced_through_chapter")
