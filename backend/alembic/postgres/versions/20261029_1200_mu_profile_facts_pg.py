"""add profile facts provenance

Revision ID: mu_profile_facts_pg
Revises: mu_world_profile_sync_state_pg
Create Date: 2026-10-29 12:00:00.000000

issue #190：档案文本列改为「原子事实 + 原文引用」的渲染产物，补上溯源列。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "mu_profile_facts_pg"
down_revision: Union[str, None] = "mu_world_profile_sync_state_pg"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "characters",
        sa.Column(
            "profile_facts",
            sa.Text(),
            nullable=True,
            comment="最近一次通过校验的档案事实(JSON 数组)",
        ),
    )
    op.add_column(
        "characters",
        sa.Column(
            "profile_facts_updated_at",
            sa.DateTime(),
            nullable=True,
            comment="档案事实最后写入时间",
        ),
    )


def downgrade() -> None:
    op.drop_column("characters", "profile_facts_updated_at")
    op.drop_column("characters", "profile_facts")
