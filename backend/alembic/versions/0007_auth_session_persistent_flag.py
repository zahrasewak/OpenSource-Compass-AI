"""auth sessions: persistent flag (client keeps the refresh token on disk)

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-02

Existing sessions get persistent = false, so their lifetimes do not change. New sessions created with
`persistent: true` use the shorter COMPASS_AUTH_PERSISTENT_* limits (docs/AUTHENTICATION.md).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0007'
down_revision: Union[str, Sequence[str], None] = '0006'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('auth_sessions') as batch:
        batch.add_column(sa.Column('persistent', sa.Boolean(), server_default=sa.false(), nullable=False))


def downgrade() -> None:
    with op.batch_alter_table('auth_sessions') as batch:
        batch.drop_column('persistent')
