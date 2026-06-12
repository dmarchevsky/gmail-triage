"""per-rule dry_run replaces the global dry-run setting

Revision ID: bc67c32f9a96
Revises: cdfd1c611254

New rules default to dry-run (safe). Existing rules inherit the behavior they
had under the global switch: if the stored global setting was live
(dry_run=false), existing rules stay live.
"""
import json

import sqlalchemy as sa

from alembic import op

revision = 'bc67c32f9a96'
down_revision = 'cdfd1c611254'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('rules', schema=None) as batch_op:
        batch_op.add_column(sa.Column('dry_run', sa.Boolean(), nullable=False,
                                      server_default=sa.true()))

    # Backfill from the (now retired) global setting, then remove it.
    conn = op.get_bind()
    row = conn.execute(sa.text(
        "SELECT value FROM settings WHERE key = 'dry_run'")).fetchone()
    if row is not None:
        raw = row[0]
        value = json.loads(raw) if isinstance(raw, str) else raw
        if value is False:
            conn.execute(sa.text("UPDATE rules SET dry_run = :v"),
                         {"v": False})
    conn.execute(sa.text(
        "DELETE FROM settings WHERE key IN ('dry_run', 'dry_run_telegram_prefix')"))


def downgrade() -> None:
    with op.batch_alter_table('rules', schema=None) as batch_op:
        batch_op.drop_column('dry_run')
