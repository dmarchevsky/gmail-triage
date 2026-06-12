"""Migration script logic, exercised SQLite->SQLite (schema creation,
FK-ordered copy with preserved ids, count verification, empty-target guard).
The Postgres sequence-reset branch is verified against the live stack."""

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "migrate_sqlite_to_pg.py"


@pytest.fixture()
def migrate_module(client):
    spec = importlib.util.spec_from_file_location("migrate_sqlite_to_pg", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["migrate_sqlite_to_pg"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def populated_source(client, db_session):
    """The client fixture's SQLite DB, with linked rows across tables."""
    from app.models import Category, Email, EmailAction, Setting

    cat = Category(name="MarketNews", criteria_md="m")
    db_session.add(cat)
    db_session.add(Setting(key="dry_run", value=False))
    db_session.flush()
    email = Email(gmail_message_id="pg1", sender="a@x.com", subject="s",
                  status="classified", classification_id=cat.id, confidence=0.9,
                  received_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC))
    db_session.add(email)
    db_session.flush()
    db_session.add(EmailAction(email_id=email.id, action_type="archive",
                               executed=True, dry_run=False))
    db_session.commit()
    from app.config import get_config

    return get_config().data_dir / "mailtriage.db"


def test_copy_preserves_rows_and_ids(migrate_module, populated_source, tmp_path):
    target = f"sqlite:///{tmp_path}/target.db"
    rc = migrate_module.migrate(str(populated_source), target, force=False)
    assert rc == 0

    from sqlalchemy import create_engine, select

    from app.models import Category, Email, EmailAction, Setting

    engine = create_engine(target)
    with Session(engine) as s:
        email = s.scalars(select(Email)).one()
        cat = s.scalars(select(Category)).one()
        action = s.scalars(select(EmailAction)).one()
        assert email.classification_id == cat.id     # FK preserved
        assert action.email_id == email.id
        assert email.received_at == datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
        assert s.get(Setting, "dry_run").value is False


def test_refuses_non_empty_target(migrate_module, populated_source, tmp_path):
    target = f"sqlite:///{tmp_path}/target.db"
    assert migrate_module.migrate(str(populated_source), target, force=False) == 0
    # second run against the now-populated target must refuse
    assert migrate_module.migrate(str(populated_source), target, force=False) == 1


def test_missing_source_fails(migrate_module, tmp_path):
    assert migrate_module.migrate(str(tmp_path / "nope.db"),
                                  f"sqlite:///{tmp_path}/t.db", force=False) == 1
