"""add agent execution steps

Revision ID: b6d3a8e1c520
Revises: 7c1a9e4b2d10
Create Date: 2026-08-18 08:59:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b6d3a8e1c520"
down_revision: Union[str, None] = "7c1a9e4b2d10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_execution_steps",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("conversation_id", sa.String(36), nullable=False),
        sa.Column("user_message_id", sa.String(36), nullable=True),
        sa.Column("assistant_message_id", sa.String(36), nullable=True),
        sa.Column("tool_call_id", sa.String(36), nullable=True),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("step_type", sa.String(30), nullable=False),
        sa.Column("category", sa.String(30), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["agent_conversations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["user_message_id"], ["agent_messages.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["assistant_message_id"], ["agent_messages.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["tool_call_id"], ["agent_tool_calls.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_agent_execution_steps_conversation_id",
        "agent_execution_steps",
        ["conversation_id"],
    )
    op.create_index(
        "ix_agent_execution_steps_user_message_id",
        "agent_execution_steps",
        ["user_message_id"],
    )
    op.create_index(
        "ix_agent_execution_steps_assistant_message_id",
        "agent_execution_steps",
        ["assistant_message_id"],
    )
    op.create_index(
        "ix_agent_execution_steps_tool_call_id",
        "agent_execution_steps",
        ["tool_call_id"],
    )


def downgrade() -> None:
    op.drop_table("agent_execution_steps")
