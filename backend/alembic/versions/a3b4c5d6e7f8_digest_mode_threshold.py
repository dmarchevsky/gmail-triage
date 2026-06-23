"""Add digests.mode and digests.email_threshold

Revision ID: a3b4c5d6e7f8
Revises: f3a8c1d2e9b4

Adds `mode` (assemble/synthesize, per-digest, replaces global digest_mode
setting) and `email_threshold` (optional email count that triggers an immediate
extra send). Existing rows get mode='assemble' and email_threshold=NULL.
"""
import sqlalchemy as sa

from alembic import op

revision = 'a3b4c5d6e7f8'
down_revision = 'f3a8c1d2e9b4'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('digests',
                  sa.Column('mode', sa.String(16), nullable=False,
                            server_default='assemble'))
    op.add_column('digests',
                  sa.Column('email_threshold', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('digests', 'email_threshold')
    op.drop_column('digests', 'mode')
