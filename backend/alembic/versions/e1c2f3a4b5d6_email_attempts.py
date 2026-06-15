"""Add emails.attempts classification-attempt counter

Revision ID: e1c2f3a4b5d6
Revises: d4e7f1a09b32

Adds an `attempts` counter (default 0) used to bound recovery retries: the
recovery sweep retries stalled/errored emails only while attempts < the
configured cap, leaving permanently-broken emails terminally in `error`.
"""
import sqlalchemy as sa

from alembic import op

revision = 'e1c2f3a4b5d6'
down_revision = 'd4e7f1a09b32'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('emails',
                  sa.Column('attempts', sa.Integer(), nullable=False,
                            server_default='0'))


def downgrade() -> None:
    op.drop_column('emails', 'attempts')
