"""repository intelligence: snapshot data column, one issue_features row per (issue, version)

Revision ID: 0005
Revises: 0004
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSONT = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    with op.batch_alter_table("repository_snapshots") as batch:
        batch.add_column(sa.Column("data", JSONT, nullable=False, server_default="{}"))
    with op.batch_alter_table("issue_features") as batch:
        batch.create_unique_constraint("uq_issue_features_issue_version", ["issue_id", "feature_version"])


def downgrade() -> None:
    with op.batch_alter_table("issue_features") as batch:
        batch.drop_constraint("uq_issue_features_issue_version", type_="unique")
    with op.batch_alter_table("repository_snapshots") as batch:
        batch.drop_column("data")
