"""merge disable thinking and agent tool support heads

Revision ID: mu_merge_disable_thinking_pg
Revises: 9f8e7d6c5b4a, f0e1d2c3b4a5
Create Date: 2026-08-29 10:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "mu_merge_disable_thinking_pg"
down_revision: Union[str, Sequence[str], None] = ("9f8e7d6c5b4a", "f0e1d2c3b4a5")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = {col["name"] for col in inspector.get_columns("settings")}
    if "disable_thinking" not in columns:
        op.add_column(
            "settings",
            sa.Column(
                "disable_thinking",
                sa.Boolean(),
                server_default="0",
                nullable=False,
                comment="是否关闭模型思考（开启后思考型模型跳过思考阶段直接输出正文）",
            ),
        )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = {col["name"] for col in inspector.get_columns("settings")}
    if "disable_thinking" in columns:
        op.drop_column("settings", "disable_thinking")
