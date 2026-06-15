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


def test_per_rule_dry_run_backfill_from_global_setting(tmp_path):
    """Upgrading from the global-dry-run era: rules inherit the stored global
    value (live stays live), and the retired settings rows are removed."""
    import json

    from alembic.config import Config as AlembicConfig
    from sqlalchemy import create_engine, text

    from alembic import command
    from app.db import BACKEND_DIR

    url = f"sqlite:///{tmp_path}/old.db"
    cfg = AlembicConfig(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "cdfd1c611254")  # last pre-per-rule revision

    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO settings (key, value) VALUES ('dry_run', :v)"),
            {"v": json.dumps(False)})  # global was LIVE
        conn.execute(text(
            "INSERT INTO rules (name, enabled, priority, match_min_confidence, "
            "actions, stop_processing, created_at, updated_at) "
            "VALUES ('r1', 1, 10, 0, '[]', 1, '2026-01-01', '2026-01-01')"))

    command.upgrade(cfg, "head")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT dry_run FROM rules")).scalar() == 0  # live
        assert conn.execute(text(
            "SELECT COUNT(*) FROM settings WHERE key IN "
            "('dry_run', 'dry_run_telegram_prefix')")).scalar() == 0


def test_labels_split_from_categories_migration(tmp_path):
    """Upgrading from pre-labels: a Label is created per category label and
    rules' add_label actions are rewritten to label_id."""
    import json

    from alembic.config import Config as AlembicConfig
    from sqlalchemy import create_engine, text

    from alembic import command
    from app.db import BACKEND_DIR

    url = f"sqlite:///{tmp_path}/pre.db"
    cfg = AlembicConfig(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "bc67c32f9a96")  # last pre-labels revision

    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO categories (name, criteria_md, criteria_version, enabled, "
            "gmail_label_name, created_at, updated_at) VALUES "
            "('MarketNews','m',1,1,'MailTriage/MarketNews','2026-01-01','2026-01-01')"))
        conn.execute(text(
            "INSERT INTO rules (name, enabled, priority, match_min_confidence, actions, "
            "stop_processing, dry_run, created_at, updated_at) VALUES "
            "('r',1,10,0,:a,1,1,'2026-01-01','2026-01-01')"),
            {"a": json.dumps([{"type": "add_label", "category_id": 1},
                              {"type": "mark_read"}])})

    command.upgrade(cfg, "head")
    with engine.connect() as conn:
        labels = conn.execute(
            text("SELECT id, name FROM labels WHERE NOT is_system")).fetchall()
        assert [(r[0], r[1]) for r in labels] == [(1, "MailTriage/MarketNews")]
        actions = json.loads(conn.execute(text("SELECT actions FROM rules")).scalar())
        assert actions == [{"type": "add_label", "label_id": 1}, {"type": "mark_read"}]
        cols = [r[1] for r in conn.execute(text("PRAGMA table_info(categories)"))]
        assert "gmail_label_name" not in cols
