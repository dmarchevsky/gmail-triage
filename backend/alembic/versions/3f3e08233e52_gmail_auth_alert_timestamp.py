"""Add gmail_auth.last_auth_alert_at for Telegram re-auth alert cooldown

Revision ID: 3f3e08233e52
Revises: c2d3e4f5a6b7

Tracks when the last "Gmail needs reconnecting" Telegram alert was sent, so
poller_loop can alert immediately on first failure, then at most once per 24h
while broken, and clear it once poll_once() succeeds again. Nullable; NULL
means no alert is currently outstanding.
"""
import sqlalchemy as sa

from alembic import op

revision = '3f3e08233e52'
down_revision = 'c2d3e4f5a6b7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('gmail_auth',
                  sa.Column('last_auth_alert_at', sa.DateTime(timezone=True),
                            nullable=True))


def downgrade() -> None:
    op.drop_column('gmail_auth', 'last_auth_alert_at')
