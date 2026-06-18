"""Add gmail_auth.watch_expiration for Gmail push (watch) lifecycle

Revision ID: f3a8c1d2e9b4
Revises: e9f1a2b3c4d5

Stores the Gmail users.watch expiration (epoch ms, as text) so the poller can
renew the watch before it lapses (Gmail expires watches within 7 days). Nullable;
NULL means no active watch (poll mode, or push mode not yet watched).
"""
import sqlalchemy as sa

from alembic import op

revision = 'f3a8c1d2e9b4'
down_revision = 'e9f1a2b3c4d5'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('gmail_auth',
                  sa.Column('watch_expiration', sa.String(length=32), nullable=True))


def downgrade() -> None:
    op.drop_column('gmail_auth', 'watch_expiration')
