"""persisted matches, feedback categories

Revision ID: 0004
Revises: 0003
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSONT = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    # batch mode: plain ALTERs on PostgreSQL, table rebuild on SQLite.
    with op.batch_alter_table("recommendations") as batch:
        batch.add_column(sa.Column("coverage", sa.Float(), nullable=True))
        batch.add_column(sa.Column("insufficient_information", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column("input_hash", sa.String(64), nullable=False, server_default=""))
        batch.add_column(sa.Column("data_snapshot", JSONT, nullable=False, server_default="{}"))
        batch.create_index("ix_recommendations_user_created", ["user_id", "created_at"])
        batch.create_unique_constraint(
            "uq_recommendations_user_issue_model_input",
            ["user_id", "issue_id", "ranking_model_version", "input_hash"],
        )

    with op.batch_alter_table("feedback") as batch:
        batch.add_column(sa.Column("category", sa.String(20), nullable=False, server_default="explicit_feedback"))
        batch.add_column(sa.Column("source", sa.String(20), nullable=False, server_default="user"))
        batch.add_column(sa.Column("recommendation_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_feedback_recommendation_id_recommendations", "recommendations",
            ["recommendation_id"], ["id"], ondelete="SET NULL",
        )
        batch.create_index("ix_feedback_recommendation_id", ["recommendation_id"])
        batch.create_index("ix_feedback_user_issue_type", ["user_id", "issue_id", "feedback_type"])


def downgrade() -> None:
    with op.batch_alter_table("feedback") as batch:
        batch.drop_index("ix_feedback_user_issue_type")
        batch.drop_index("ix_feedback_recommendation_id")
        batch.drop_constraint("fk_feedback_recommendation_id_recommendations", type_="foreignkey")
        batch.drop_column("recommendation_id")
        batch.drop_column("source")
        batch.drop_column("category")
    with op.batch_alter_table("recommendations") as batch:
        batch.drop_constraint("uq_recommendations_user_issue_model_input", type_="unique")
        batch.drop_index("ix_recommendations_user_created")
        batch.drop_column("data_snapshot")
        batch.drop_column("input_hash")
        batch.drop_column("insufficient_information")
        batch.drop_column("coverage")
