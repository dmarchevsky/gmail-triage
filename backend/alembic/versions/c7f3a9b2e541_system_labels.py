"""Add is_system column and seed Important, Starred, Spam system labels

Revision ID: c7f3a9b2e541
Revises: 4ed436dc1622

Gmail's IMPORTANT, STARRED and SPAM label IDs are built-in; they exist in
every Gmail account and cannot be created or deleted. Seeding them here lets
rules reference them via the normal add_label/remove_label actions.
"""
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision = 'c7f3a9b2e541'
down_revision = '4ed436dc1622'
branch_labels = None
depends_on = None

_SYSTEM_LABELS = [
    ("Important", "IMPORTANT"),
    ("Starred",   "STARRED"),
    ("Spam",      "SPAM"),
]


def upgrade() -> None:
    op.add_column('labels', sa.Column('is_system', sa.Boolean(),
                                      nullable=False, server_default='0'))

    conn = op.get_bind()
    now = datetime.now(UTC)
    labels_table = sa.table(
        'labels',
        sa.column('name', sa.String),
        sa.column('gmail_label_id', sa.String),
        sa.column('is_system', sa.Boolean),
        sa.column('created_at', sa.DateTime(timezone=True)),
        sa.column('updated_at', sa.DateTime(timezone=True)),
    )
    existing = {row[0] for row in conn.execute(
        sa.select(sa.column('name')).select_from(sa.table('labels')))}
    for name, gmail_id in _SYSTEM_LABELS:
        if name not in existing:
            conn.execute(labels_table.insert().values(
                name=name, gmail_label_id=gmail_id, is_system=True,
                created_at=now, updated_at=now))


def downgrade() -> None:
    op.drop_column('labels', 'is_system')
