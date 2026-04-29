"""添加后台任务表

Revision ID: def45678ghi9
Revises: ab12cd34ef56
Create Date: 2026-04-27 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'def45678ghi9'
down_revision = 'ab12cd34ef56'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'background_tasks',
        sa.Column('id', sa.String(), primary_key=True),
        sa.Column('task_type', sa.String(), nullable=False, index=True),
        sa.Column('project_id', sa.String(), nullable=False, index=True),
        sa.Column('user_id', sa.String(), nullable=True),
        sa.Column('status', sa.String(), default='pending', nullable=False, index=True),
        sa.Column('progress', sa.Float(), default=0.0),
        sa.Column('status_message', sa.String(), nullable=True),
        sa.Column('progress_details', sa.Text(), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('task_params', sa.Text(), nullable=True),
        sa.Column('task_result', sa.Text(), nullable=True),
        sa.Column('cancel_requested', sa.Boolean(), default=False),
        sa.Column('retry_count', sa.Integer(), default=0),
        sa.Column('max_retries', sa.Integer(), default=3),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), onupdate=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table('background_tasks')