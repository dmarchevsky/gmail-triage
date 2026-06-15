"""Add processing_started_at and processing email status

Revision ID: d4e7f1a09b32
Revises: c7f3a9b2e541

Adds processing_started_at to track when classification started per email.
Any emails stuck in 'processing' from a prior crash are reset to 'pending'.
"""
import sqlalchemy as sa

from alembic import op

revision = 'd4e7f1a09b32'
down_revision = 'c7f3a9b2e541'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('emails',
                  sa.Column('processing_started_at', sa.DateTime(timezone=True),
                            nullable=True))
    # Safety: reset any rows stuck in processing from a prior crash
    op.execute("UPDATE emails SET status='pending', processing_started_at=NULL "
               "WHERE status='processing'")


def downgrade() -> None:
    op.drop_column('emails', 'processing_started_at')
