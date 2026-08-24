"""Add source category tracking to Feedback for bidirectional proposal support

Revision ID: p3y1ys3kd274
Revises: d7a4e1f9c2b3

When a user provides feedback to correct an email's classification, we now track
not only the target category (correct_category_id) but also the source category
(source_category_id). This enables generating proposals to refine the *losing*
category's criteria, not just the winning category's. The source category is
snapshotted at feedback creation so it persists even if the email is later
reclassified.

Backfills source_category_id on existing feedback rows by joining with emails
where the feedback's correct_category_id differs from the email's classification_id,
making existing feedback immediately eligible for source-exclusion proposal generation.
"""

import sqlalchemy as sa

from alembic import op

revision = 'p3y1ys3kd274'
down_revision = 'd7a4e1f9c2b3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('feedback', schema=None) as batch_op:
        batch_op.add_column(sa.Column('source_category_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('proposed_source_criteria_md', sa.Text(),
                                     nullable=True))
        batch_op.add_column(sa.Column('proposal_source_explanation', sa.Text(),
                                     nullable=True))
        batch_op.add_column(sa.Column('proposal_source_status', sa.String(16),
                                     nullable=False, server_default='none'))
        batch_op.add_column(sa.Column('proposal_source_feedback_ids', sa.JSON(),
                                     nullable=True))
        batch_op.create_foreign_key(
            op.f('fk_feedback_source_category_id_categories'),
            'categories', ['source_category_id'], ['id'],
            ondelete='SET NULL'
        )

    # Backfill source_category_id from emails.classification_id where feedback
    # has a correct_category_id that differs from the email's classification
    conn = op.get_bind()
    feedback_tbl = sa.table(
        'feedback',
        sa.column('email_id', sa.Integer),
        sa.column('correct_category_id', sa.Integer),
        sa.column('source_category_id', sa.Integer),
    )
    emails_tbl = sa.table(
        'emails',
        sa.column('id', sa.Integer),
        sa.column('classification_id', sa.Integer),
    )

    # Update feedback rows where correct_category_id != emails.classification_id
    # (only backfill rows with an actual source/target category conflict)
    conn.execute(
        feedback_tbl.update()
        .where(
            (feedback_tbl.c.correct_category_id.isnot(None)) &
            (feedback_tbl.c.correct_category_id != sa.select(
                emails_tbl.c.classification_id
            ).where(emails_tbl.c.id == feedback_tbl.c.email_id)
             .correlate(feedback_tbl).scalar_subquery())
        )
        .values(
            source_category_id=sa.select(emails_tbl.c.classification_id)
            .where(emails_tbl.c.id == feedback_tbl.c.email_id)
            .correlate(feedback_tbl)
            .scalar_subquery()
        )
    )


def downgrade() -> None:
    with op.batch_alter_table('feedback', schema=None) as batch_op:
        batch_op.drop_constraint(op.f('fk_feedback_source_category_id_categories'),
                                 type_='foreignkey')
        batch_op.drop_column('proposal_source_feedback_ids')
        batch_op.drop_column('proposal_source_status')
        batch_op.drop_column('proposal_source_explanation')
        batch_op.drop_column('proposed_source_criteria_md')
        batch_op.drop_column('source_category_id')
