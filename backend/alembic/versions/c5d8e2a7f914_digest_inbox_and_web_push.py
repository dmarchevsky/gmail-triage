"""digest delivery channels, in-app inbox, persisted content, web push subscriptions

Revision ID: c5d8e2a7f914
Revises: a4c9e2f7b1d8

"""
import sqlalchemy as sa

from alembic import op

revision = 'c5d8e2a7f914'
down_revision = 'a4c9e2f7b1d8'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing digests keep delivering to Telegram; in-app is opt-in.
    with op.batch_alter_table('digests', schema=None) as batch_op:
        batch_op.add_column(sa.Column('notify_telegram', sa.Boolean(),
                                      nullable=False, server_default=sa.true()))
        batch_op.add_column(sa.Column('notify_inapp', sa.Boolean(),
                                      nullable=False, server_default=sa.false()))

    with op.batch_alter_table('digest_runs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('content', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('notified_inapp', sa.Boolean(),
                                      nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column('read_at', sa.DateTime(timezone=True),
                                      nullable=True))
        # A multi-part Telegram digest stores a comma-joined id list that can
        # exceed 64 chars (Postgres enforces VARCHAR length).
        batch_op.alter_column('telegram_message_id', existing_type=sa.String(length=64),
                              type_=sa.Text(), existing_nullable=True)

    op.create_table(
        'push_subscriptions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('endpoint', sa.Text(), nullable=False),
        sa.Column('p256dh', sa.String(length=128), nullable=False),
        sa.Column('auth', sa.String(length=64), nullable=False),
        sa.Column('user_agent', sa.String(length=512), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_success_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('failure_count', sa.Integer(), nullable=False, server_default='0'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('endpoint'),
    )


def downgrade() -> None:
    op.drop_table('push_subscriptions')
    with op.batch_alter_table('digest_runs', schema=None) as batch_op:
        batch_op.alter_column('telegram_message_id', existing_type=sa.Text(),
                              type_=sa.String(length=64), existing_nullable=True)
        batch_op.drop_column('read_at')
        batch_op.drop_column('notified_inapp')
        batch_op.drop_column('content')
    with op.batch_alter_table('digests', schema=None) as batch_op:
        batch_op.drop_column('notify_inapp')
        batch_op.drop_column('notify_telegram')
