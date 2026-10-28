"""add character profile sync state

Revision ID: mu_profile_sync_state_pg
Revises: mu_relationship_multi_type_pg
Create Date: 2026-10-28 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "mu_profile_sync_state_pg"
down_revision: Union[str, None] = "mu_relationship_multi_type_pg"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "characters",
        sa.Column(
            "profile_synced_through_chapter",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="档案已同步到的章节号",
        ),
    )
    op.add_column(
        "characters",
        sa.Column(
            "ai_profile_snapshot",
            sa.Text(),
            nullable=True,
            comment="上一次 AI 写入的档案字段快照(JSON)",
        ),
    )


def downgrade() -> None:
    op.drop_column("characters", "ai_profile_snapshot")
    op.drop_column("characters", "profile_synced_through_chapter")
