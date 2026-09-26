"""drop password-auth settings (auth moved to Cloudflare Access)

Revision ID: a4c9e2f7b1d8
Revises: e1a2c9f4b7d3

"""
import sqlalchemy as sa

from alembic import op

revision = 'a4c9e2f7b1d8'
down_revision = 'e1a2c9f4b7d3'
branch_labels = None
depends_on = None

settings = sa.table('settings', sa.column('key', sa.String))


def upgrade() -> None:
    op.execute(settings.delete().where(
        settings.c.key.in_(['ui_password_hash', 'auth_disabled'])))


def downgrade() -> None:
    pass  # the removed rows only held the retired UI password; nothing to restore
