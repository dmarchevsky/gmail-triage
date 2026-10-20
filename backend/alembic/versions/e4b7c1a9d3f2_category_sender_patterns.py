"""Category sender pins: senders that always classify into a category

Revision ID: e4b7c1a9d3f2
Revises: c5d8e2a7f914

`categories.sender_patterns` is a JSON list of sender patterns (same glob /
substring / regex semantics as the ignore list). A pinned sender skips the
model entirely and is classified into the category with confidence 1.0.
"""

import sqlalchemy as sa

from alembic import op

revision = 'e4b7c1a9d3f2'
down_revision = 'c5d8e2a7f914'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('categories', schema=None) as batch_op:
        batch_op.add_column(sa.Column('sender_patterns', sa.JSON(), nullable=True,
                                      server_default=sa.text("'[]'")))
    categories = sa.table('categories', sa.column('sender_patterns', sa.JSON))
    op.get_bind().execute(
        categories.update().where(categories.c.sender_patterns.is_(None))
        .values(sender_patterns=[]))


def downgrade() -> None:
    with op.batch_alter_table('categories', schema=None) as batch_op:
        batch_op.drop_column('sender_patterns')
