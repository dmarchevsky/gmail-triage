"""Add rules.is_default and seed the catch-all default rule

Revision ID: a1b2c3d4e5f6
Revises: f2d3e4a5b6c7

Adds an `is_default` flag and seeds the single catch-all rule. It is always
evaluated last and fires only when no other rule matched. It ships with no
actions (a no-op) and in dry-run; it cannot be deleted or reordered.
"""
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision = 'a1b2c3d4e5f6'
down_revision = 'f2d3e4a5b6c7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('rules', sa.Column('is_default', sa.Boolean(),
                                     nullable=False, server_default='0'))

    conn = op.get_bind()
    has_default = conn.execute(sa.select(sa.func.count()).select_from(
        sa.table('rules', sa.column('is_default', sa.Boolean))).where(
        sa.column('is_default') == True)).scalar()  # noqa: E712
    if has_default:
        return

    now = datetime.now(UTC)
    rules_table = sa.table(
        'rules',
        sa.column('name', sa.String),
        sa.column('enabled', sa.Boolean),
        sa.column('priority', sa.Integer),
        sa.column('match_min_confidence', sa.Float),
        sa.column('actions', sa.JSON),
        sa.column('stop_processing', sa.Boolean),
        sa.column('dry_run', sa.Boolean),
        sa.column('is_default', sa.Boolean),
        sa.column('created_at', sa.DateTime(timezone=True)),
        sa.column('updated_at', sa.DateTime(timezone=True)),
    )
    conn.execute(rules_table.insert().values(
        name='Default', enabled=True, priority=1000000,
        match_min_confidence=0.0, actions=[],
        stop_processing=True, dry_run=True, is_default=True,
        created_at=now, updated_at=now))


def downgrade() -> None:
    op.drop_column('rules', 'is_default')
