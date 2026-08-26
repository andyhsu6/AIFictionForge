"""add analysis task archive state

Revision ID: f1a2b3c4d5e6
Revises: b6d3a8e1c520
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f1a2b3c4d5e6"
down_revision: Union[str, None] = "b6d3a8e1c520"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("analysis_tasks", sa.Column("archived_at", sa.DateTime(), nullable=True))
    op.create_index(
        "idx_analysis_task_panel",
        "analysis_tasks",
        ["project_id", "user_id", "archived_at", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_analysis_task_panel", table_name="analysis_tasks")
    op.drop_column("analysis_tasks", "archived_at")
