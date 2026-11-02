"""repository/issue columns needed for GitHub synchronisation

Revision ID: 0003
Revises: 0002
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSONT = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.add_column("repositories", sa.Column("archived", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("repositories", sa.Column("stargazers_count", sa.Integer(), nullable=True))
    op.add_column("repositories", sa.Column("forks_count", sa.Integer(), nullable=True))
    op.add_column("repositories", sa.Column("open_issues_count", sa.Integer(), nullable=True))
    op.add_column("repositories", sa.Column("license_spdx", sa.String(50), nullable=True))
    op.add_column("repositories", sa.Column("topics", JSONT, nullable=False, server_default="[]"))
    op.add_column("repositories", sa.Column("pushed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("repositories", sa.Column("issues_synced_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("repositories", sa.Column("issues_truncated", sa.Boolean(), nullable=True))

    op.add_column("issues", sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("issues", sa.Column("author", sa.String(100), nullable=True))
    op.add_column("issues", sa.Column("author_association", sa.String(30), nullable=True))
    op.add_column("issues", sa.Column("assignees", JSONT, nullable=False, server_default="[]"))
    op.add_column("issues", sa.Column("comments_count", sa.Integer(), nullable=True))
    op.add_column(
        "issues",
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    with op.batch_alter_table("issues") as batch:
        for col in ("fetched_at", "comments_count", "assignees", "author_association", "author", "closed_at"):
            batch.drop_column(col)
    with op.batch_alter_table("repositories") as batch:
        for col in ("issues_truncated", "issues_synced_at", "pushed_at", "topics", "license_spdx",
                    "open_issues_count", "forks_count", "stargazers_count", "archived"):
            batch.drop_column(col)
