"""Set colors on system labels (Important, Starred, Spam)

Revision ID: e9f1a2b3c4d5
Revises: d5b8a2c1e6f4
Branch labels: None
Depends on: None
"""
import sqlalchemy as sa

from alembic import op

revision = 'e9f1a2b3c4d5'
down_revision = 'd5b8a2c1e6f4'
branch_labels = None
depends_on = None

_COLORS = {
    "Important": {"text_color": "#92400e", "background_color": "#fef3c7"},
    "Starred":   {"text_color": "#713f12", "background_color": "#fef9c3"},
    "Spam":      {"text_color": "#991b1b", "background_color": "#fee2e2"},
}


def upgrade() -> None:
    conn = op.get_bind()
    labels = sa.table(
        'labels',
        sa.column('name', sa.String),
        sa.column('text_color', sa.String),
        sa.column('background_color', sa.String),
    )
    for name, colors in _COLORS.items():
        conn.execute(
            sa.update(labels)
            .where(sa.column('name') == name)
            .values(**colors)
        )


def downgrade() -> None:
    conn = op.get_bind()
    labels = sa.table(
        'labels',
        sa.column('name', sa.String),
        sa.column('text_color', sa.String),
        sa.column('background_color', sa.String),
    )
    for name in _COLORS:
        conn.execute(
            sa.update(labels)
            .where(sa.column('name') == name)
            .values(text_color=None, background_color=None)
        )
