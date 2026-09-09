"""digest collapsed_sections column

Revision ID: e1a2c9f4b7d3
Revises: bbb10ae809a3

"""
import sqlalchemy as sa

from alembic import op

revision = 'e1a2c9f4b7d3'
down_revision = 'bbb10ae809a3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('digests', schema=None) as batch_op:
        batch_op.add_column(sa.Column('collapsed_sections', sa.Boolean(),
                                      nullable=False, server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table('digests', schema=None) as batch_op:
        batch_op.drop_column('collapsed_sections')
