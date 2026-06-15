"""Add digests.depth detail level

Revision ID: f2d3e4a5b6c7
Revises: e1c2f3a4b5d6

Adds a `depth` column (1 brief · 2 standard · 3 detailed) controlling how much
per-email body is fed to the LLM and how verbose the synthesis is. Existing
rows default to 2 (standard, the prior behavior).
"""
import sqlalchemy as sa

from alembic import op

revision = 'f2d3e4a5b6c7'
down_revision = 'e1c2f3a4b5d6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('digests',
                  sa.Column('depth', sa.Integer(), nullable=False,
                            server_default='2'))


def downgrade() -> None:
    op.drop_column('digests', 'depth')
