"""Add emails.summary; drop digests.depth

Revision ID: b7e1c9d2f4a3
Revises: a1b2c3d4e5f6

Summaries are now produced once at classification time and stored on the email,
so digests can be built from them without re-summarizing per run. The per-digest
`depth` knob is replaced by a system-wide summarization depth setting and is
dropped here. `batch_alter_table` keeps the column drop SQLite-compatible.
"""
import sqlalchemy as sa

from alembic import op

revision = 'b7e1c9d2f4a3'
down_revision = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('emails', sa.Column('summary', sa.Text(), nullable=True))
    with op.batch_alter_table('digests') as batch:
        batch.drop_column('depth')


def downgrade() -> None:
    op.add_column('digests',
                  sa.Column('depth', sa.Integer(), nullable=False,
                            server_default='2'))
    with op.batch_alter_table('emails') as batch:
        batch.drop_column('summary')
