"""add project scoped relationship types and multi-type links

Revision ID: mu_relationship_multi_type
Revises: mu_merge_disable_thinking
Create Date: 2026-08-29 11:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "mu_relationship_multi_type"
down_revision: Union[str, None] = "mu_merge_disable_thinking"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("relationship_types", schema=None) as batch_op:
        batch_op.add_column(sa.Column("project_id", sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column("source", sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column("is_system", sa.Boolean(), nullable=True))
        batch_op.create_index("ix_relationship_types_project_id", ["project_id"])
        batch_op.create_unique_constraint("uq_relationship_types_project_name", ["project_id", "name"])

    op.execute("UPDATE relationship_types SET source='system', is_system=1 WHERE is_system IS NULL")
    op.execute("UPDATE relationship_types SET source='manual' WHERE source IS NULL")
    op.execute("UPDATE relationship_types SET is_system=0 WHERE is_system IS NULL")

    with op.batch_alter_table("characters", schema=None) as batch_op:
        batch_op.add_column(sa.Column("source", sa.String(length=20), nullable=True))

    op.create_table(
        "character_relationship_type_links",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("relationship_id", sa.String(length=36), nullable=False),
        sa.Column("relationship_type_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["relationship_id"], ["character_relationships.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["relationship_type_id"], ["relationship_types.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("relationship_id", "relationship_type_id", name="uq_relationship_type_links_pair"),
    )
    op.create_index("ix_relationship_type_links_relationship", "character_relationship_type_links", ["relationship_id"])
    op.create_index("ix_relationship_type_links_type", "character_relationship_type_links", ["relationship_type_id"])

    op.execute(
        """
        INSERT INTO character_relationship_type_links (relationship_id, relationship_type_id, created_at)
        SELECT id, relationship_type_id, CURRENT_TIMESTAMP
        FROM character_relationships
        WHERE relationship_type_id IS NOT NULL
        """
    )


def downgrade() -> None:
    op.drop_table("character_relationship_type_links")
    with op.batch_alter_table("characters", schema=None) as batch_op:
        batch_op.drop_column("source")
    with op.batch_alter_table("relationship_types", schema=None) as batch_op:
        batch_op.drop_constraint("uq_relationship_types_project_name", type_="unique")
        batch_op.drop_index("ix_relationship_types_project_id")
        batch_op.drop_column("is_system")
        batch_op.drop_column("source")
        batch_op.drop_column("project_id")
