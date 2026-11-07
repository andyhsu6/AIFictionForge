"""add structured style profile and source anchors

Revision ID: mu_style_profile_p1
Revises: mu_world_facts
Create Date: 2026-11-08 12:00:00.000000

issue #254 P1/P2：
- `writing_styles` 增加结构化画像列（profile_json / profile_version），
  供重新抽取 diff、导出 1.4.0 与一致性指标使用；
- `projects` 增加锚点注入选项（默认开启，项目级可关闭/调预算）；
- 新表 `project_style_anchors` 只存章节偏移与哈希，不复制原文。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "mu_style_profile_p1"
down_revision: Union[str, None] = "mu_world_facts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("writing_styles", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "profile_json",
                sa.Text(),
                nullable=True,
                comment="结构化文笔画像(JSON，issue #254)",
            )
        )
        batch_op.add_column(
            sa.Column(
                "profile_version",
                sa.Integer(),
                nullable=True,
                comment="画像结构版本",
            )
        )

    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "style_anchor_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("1"),
                comment="生成时是否注入本书原文语感锚点",
            )
        )
        batch_op.add_column(
            sa.Column(
                "style_anchor_chars",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("800"),
                comment="锚点总字符预算（上限 2000）",
            )
        )
        batch_op.add_column(
            sa.Column(
                "style_anchor_count",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("3"),
                comment="锚点条数（2-4）",
            )
        )

    op.create_table(
        "project_style_anchors",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "project_id",
            sa.String(length=36),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "chapter_id",
            sa.String(length=36),
            sa.ForeignKey("chapters.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("start_offset", sa.Integer(), nullable=False),
        sa.Column("end_offset", sa.Integer(), nullable=False),
        sa.Column("text_hash", sa.String(length=64), nullable=False),
        sa.Column("order_index", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index(
        "ix_project_style_anchors_project_id",
        "project_style_anchors",
        ["project_id"],
    )
    op.create_index(
        "ix_project_style_anchors_chapter_id",
        "project_style_anchors",
        ["chapter_id"],
    )
    op.create_index(
        "ix_project_style_anchors_project_order",
        "project_style_anchors",
        ["project_id", "order_index"],
    )


def downgrade() -> None:
    op.drop_index("ix_project_style_anchors_project_order", table_name="project_style_anchors")
    op.drop_index("ix_project_style_anchors_chapter_id", table_name="project_style_anchors")
    op.drop_index("ix_project_style_anchors_project_id", table_name="project_style_anchors")
    op.drop_table("project_style_anchors")

    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.drop_column("style_anchor_count")
        batch_op.drop_column("style_anchor_chars")
        batch_op.drop_column("style_anchor_enabled")

    with op.batch_alter_table("writing_styles", schema=None) as batch_op:
        batch_op.drop_column("profile_version")
        batch_op.drop_column("profile_json")
