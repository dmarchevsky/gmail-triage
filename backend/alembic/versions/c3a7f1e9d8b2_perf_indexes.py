"""Add performance indexes for stats/list/rules queries

Revision ID: c3a7f1e9d8b2
Revises: b7e1c9d2f4a3

Adds indexes that back hot read paths that previously full-scanned:
- emails.created_at — the /stats dashboard filters every aggregate on it.
- emails.classification_id — per-category stats + email-list filtering.
- email_actions.rule_id — the per-rule pending-planned COUNT in /rules.

Index names match SQLAlchemy's `index=True` convention (ix_<table>_<column>)
so the ORM models and the migrated schema agree.
"""
from alembic import op

revision = 'c3a7f1e9d8b2'
down_revision = 'b7e1c9d2f4a3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index('ix_emails_created_at', 'emails', ['created_at'])
    op.create_index('ix_emails_classification_id', 'emails', ['classification_id'])
    op.create_index('ix_email_actions_rule_id', 'email_actions', ['rule_id'])


def downgrade() -> None:
    op.drop_index('ix_email_actions_rule_id', table_name='email_actions')
    op.drop_index('ix_emails_classification_id', table_name='emails')
    op.drop_index('ix_emails_created_at', table_name='emails')
