"""labels become first-class entities, separate from categories

Revision ID: a8f28ca13ed0
Revises: bc67c32f9a96

Creates the labels table, backfills a Label from each category's
gmail_label_name (null -> MailTriage/<name>, preserving prior behavior),
rewrites every rule's add_label/remove_label action from
{category_id|label_name} to {label_id}, then drops categories.gmail_label_name.
"""
import json
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op
from app.models import Label, Rule

revision = 'a8f28ca13ed0'
down_revision = 'bc67c32f9a96'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'labels',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('gmail_label_id', sa.String(length=64), nullable=True),
        sa.Column('text_color', sa.String(length=16), nullable=True),
        sa.Column('background_color', sa.String(length=16), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name'),
    )

    conn = op.get_bind()
    now = datetime.now(UTC)
    name_to_label_id: dict[str, int] = {}
    cat_to_label_id: dict[int, int] = {}

    def get_or_create(label_name: str) -> int:
        if label_name in name_to_label_id:
            return name_to_label_id[label_name]
        res = conn.execute(sa.insert(Label.__table__).values(
            name=label_name, created_at=now, updated_at=now))
        lid = res.inserted_primary_key[0]
        name_to_label_id[label_name] = lid
        return lid

    for cid, cname, glabel in conn.execute(sa.text(
            "SELECT id, name, gmail_label_name FROM categories")):
        cat_to_label_id[cid] = get_or_create(glabel or f"MailTriage/{cname}")

    for rid, actions in conn.execute(sa.select(Rule.__table__.c.id,
                                               Rule.__table__.c.actions)):
        acts = json.loads(actions) if isinstance(actions, str) else actions
        if not acts:
            continue
        new_acts = []
        changed = False
        for a in acts:
            if a.get("type") not in ("add_label", "remove_label"):
                new_acts.append(a)
                continue
            if a.get("label_id") is not None:
                new_acts.append(a)
                continue
            changed = True
            label_id = None
            if a.get("category_id") is not None:
                label_id = cat_to_label_id.get(a["category_id"])
            elif a.get("label_name"):
                label_id = get_or_create(a["label_name"])
            if label_id is not None:
                new_acts.append({"type": a["type"], "label_id": label_id})
            # else: unresolvable (category since deleted) -> drop the action
        if changed:
            conn.execute(sa.update(Rule.__table__)
                         .where(Rule.__table__.c.id == rid)
                         .values(actions=new_acts))

    with op.batch_alter_table('categories', schema=None) as batch_op:
        batch_op.drop_column('gmail_label_name')


def downgrade() -> None:
    with op.batch_alter_table('categories', schema=None) as batch_op:
        batch_op.add_column(sa.Column('gmail_label_name', sa.VARCHAR(length=255),
                                      nullable=True))
    op.drop_table('labels')
