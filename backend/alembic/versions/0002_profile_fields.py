"""profile fields: skill category, contribution goals, experience level

Revision ID: 0002
Revises: 0001
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("user_skills", sa.Column("category", sa.String(20), nullable=False, server_default="skill"))
    op.add_column(
        "user_preferences",
        sa.Column(
            "contribution_goals",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
            server_default="[]",
        ),
    )
    op.add_column("user_preferences", sa.Column("experience_level", sa.String(20), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("user_preferences") as batch:
        batch.drop_column("experience_level")
        batch.drop_column("contribution_goals")
    with op.batch_alter_table("user_skills") as batch:
        batch.drop_column("category")
