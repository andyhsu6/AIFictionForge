"""add project scoped relationship types and multi-type links

Revision ID: mu_relationship_multi_type_pg
Revises: mu_merge_disable_thinking_pg
Create Date: 2026-08-29 11:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "mu_relationship_multi_type_pg"
down_revision: Union[str, None] = "mu_merge_disable_thinking_pg"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("relationship_types", sa.Column("project_id", sa.String(length=36), nullable=True))
    op.add_column("relationship_types", sa.Column("source", sa.String(length=20), nullable=True))
    op.add_column("relationship_types", sa.Column("is_system", sa.Boolean(), nullable=True))
    op.create_index("ix_relationship_types_project_id", "relationship_types", ["project_id"])
    op.create_unique_constraint("uq_relationship_types_project_name", "relationship_types", ["project_id", "name"])

    op.execute("UPDATE relationship_types SET source='system', is_system=true WHERE is_system IS NULL")
    op.execute("UPDATE relationship_types SET source='manual' WHERE source IS NULL")
    op.execute("UPDATE relationship_types SET is_system=false WHERE is_system IS NULL")

    op.add_column("characters", sa.Column("source", sa.String(length=20), nullable=True))

    op.create_table(
        "character_relationship_type_links",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("relationship_id", sa.String(length=36), nullable=False),
        sa.Column("relationship_type_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["relationship_id"], ["character_relationships.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["relationship_type_id"], ["relationship_types.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("relationship_id", "relationship_type_id", name="uq_relationship_type_links_pair"),
    )
    op.create_index("ix_relationship_type_links_relationship", "character_relationship_type_links", ["relationship_id"])
    op.create_index("ix_relationship_type_links_type", "character_relationship_type_links", ["relationship_type_id"])

    op.execute(
        """
        INSERT INTO character_relationship_type_links (relationship_id, relationship_type_id, created_at)
        SELECT id, relationship_type_id, now()
        FROM character_relationships
        WHERE relationship_type_id IS NOT NULL
        """
    )


def downgrade() -> None:
    op.drop_table("character_relationship_type_links")
    op.drop_column("characters", "source")
    op.drop_constraint("uq_relationship_types_project_name", "relationship_types", type_="unique")
    op.drop_index("ix_relationship_types_project_id", table_name="relationship_types")
    op.drop_column("relationship_types", "is_system")
    op.drop_column("relationship_types", "source")
    op.drop_column("relationship_types", "project_id")
