"""auth throttle events (database-backed rate limiting shared by all workers)

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-02

One row per recent attempt/failure against an unauthenticated auth endpoint, keyed by endpoint scope and client address.
Rows are short lived (the limiter deletes anything older than the longest window). Only used when
COMPASS_AUTH_RATE_LIMIT_BACKEND=database. Contains no credentials: scope, client address, kind and a timestamp.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0008'
down_revision: Union[str, Sequence[str], None] = '0007'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('auth_throttle_events',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('scope', sa.String(length=20), nullable=False),
    sa.Column('client', sa.String(length=64), nullable=False),
    sa.Column('kind', sa.String(length=10), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_auth_throttle_events')),
    )
    op.create_index('ix_auth_throttle_lookup', 'auth_throttle_events', ['scope', 'client', 'kind', 'created_at'], unique=False)
    op.create_index(op.f('ix_auth_throttle_events_created_at'), 'auth_throttle_events', ['created_at'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_auth_throttle_events_created_at'), table_name='auth_throttle_events')
    op.drop_index('ix_auth_throttle_lookup', table_name='auth_throttle_events')
    op.drop_table('auth_throttle_events')
