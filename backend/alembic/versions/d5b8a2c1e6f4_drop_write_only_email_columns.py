"""Drop write-only emails.history_id and emails.size_estimate

Revision ID: d5b8a2c1e6f4
Revises: c3a7f1e9d8b2

Both columns were populated on ingest from the Gmail message metadata but never
read anywhere (the sync cursor lives on gmail_auth.history_id, which is
unaffected). Dropping them removes dead schema. `batch_alter_table` keeps the
drop SQLite-compatible; the downgrade re-adds the (empty) columns.
"""
import sqlalchemy as sa

from alembic import op

revision = 'd5b8a2c1e6f4'
down_revision = 'c3a7f1e9d8b2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('emails') as batch:
        batch.drop_column('history_id')
        batch.drop_column('size_estimate')


def downgrade() -> None:
    op.add_column('emails', sa.Column('size_estimate', sa.Integer(), nullable=True))
    op.add_column('emails', sa.Column('history_id', sa.String(length=64), nullable=True))
