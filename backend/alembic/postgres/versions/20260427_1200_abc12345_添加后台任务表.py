"""添加后台任务表

Revision ID: abc12345
Revises: 
Create Date: 2026-04-27 12:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSON, JSONB

# revision identifiers
revision = 'abc12345'
down_revision = '9a1b2c3d4e5f'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'background_tasks',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('user_id', sa.String(100), nullable=False, index=True, comment='用户ID'),
        sa.Column('project_id', sa.String(36), nullable=False, index=True, comment='项目ID'),
        sa.Column('task_type', sa.String(50), nullable=False, comment='任务类型'),
        sa.Column('status', sa.String(20), default='pending', comment='任务状态'),
        sa.Column('progress', sa.Integer, default=0, comment='进度百分比'),
        sa.Column('status_message', sa.String(500), comment='当前状态消息'),
        sa.Column('task_input', JSON, comment='任务输入参数'),
        sa.Column('task_result', JSON, comment='任务结果'),
        sa.Column('error_message', sa.Text, comment='错误信息'),
        sa.Column('progress_details', JSON, comment='进度详情'),
        sa.Column('cancel_requested', sa.Boolean, default=False, comment='是否请求取消'),
        sa.Column('retry_count', sa.Integer, default=0, comment='已重试次数'),
        sa.Column('max_retries', sa.Integer, default=3, comment='最大重试次数'),
        sa.Column('created_at', sa.DateTime, server_default=sa.func.now(), comment='创建时间'),
        sa.Column('started_at', sa.DateTime, comment='开始时间'),
        sa.Column('completed_at', sa.DateTime, comment='完成时间'),
        sa.Column('updated_at', sa.DateTime, server_default=sa.func.now(), onupdate=sa.func.now(), comment='更新时间'),
    )
    # 添加复合索引：按用户+项目+状态查询
    op.create_index('ix_background_tasks_user_project', 'background_tasks', ['user_id', 'project_id', 'status'])


def downgrade() -> None:
    op.drop_index('ix_background_tasks_user_project', table_name='background_tasks')
    op.drop_table('background_tasks')