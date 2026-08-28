"""add agent message tool support

Revision ID: 9f8e7d6c5b4a
Revises: f1a2b3c4d5e6
Create Date: 2026-08-28 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9f8e7d6c5b4a'
down_revision: Union[str, None] = 'f1a2b3c4d5e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('agent_messages', sa.Column('tool_calls', sa.Text(), nullable=True))
    op.add_column('agent_messages', sa.Column('tool_call_id', sa.String(length=36), nullable=True))
    op.create_index('idx_agent_messages_tool_call_id', 'agent_messages', ['tool_call_id'])


def downgrade() -> None:
    op.drop_index('idx_agent_messages_tool_call_id', table_name='agent_messages')
    op.drop_column('agent_messages', 'tool_call_id')
    op.drop_column('agent_messages', 'tool_calls')
