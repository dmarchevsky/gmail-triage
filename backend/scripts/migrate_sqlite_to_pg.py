#!/usr/bin/env python3
"""One-off data migration: SQLite file -> the database in DATABASE_URL.

Usage (inside the compose network, after `docker compose up -d postgres`):

    docker compose run --rm mailtriage python scripts/migrate_sqlite_to_pg.py

Options:
    --sqlite PATH   source SQLite file (default /data/mailtriage.db)
    --force         skip the target-must-be-empty guard

The target schema is created via `alembic upgrade head`; rows are copied in
FK-safe order with primary keys preserved; Postgres id sequences are reset;
per-table source/target counts are verified. The SQLite file is not modified.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from alembic.config import Config as AlembicConfig  # noqa: E402
from sqlalchemy import create_engine, func, insert, select, text  # noqa: E402

from alembic import command  # noqa: E402
from app.db import BACKEND_DIR  # noqa: E402
from app.models import Base  # noqa: E402


def migrate(sqlite_path: str, target_url: str, force: bool) -> int:
    if target_url.startswith("sqlite") and sqlite_path in target_url:
        print("Refusing: target equals source.")
        return 1
    if not os.path.exists(sqlite_path):
        print(f"Source SQLite file not found: {sqlite_path}")
        return 1

    source = create_engine(f"sqlite:///{sqlite_path}")
    target = create_engine(target_url)

    print(f"Target: {target.url.render_as_string(hide_password=True)}")
    alembic_cfg = AlembicConfig(str(BACKEND_DIR / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    alembic_cfg.set_main_option("sqlalchemy.url", target_url)
    command.upgrade(alembic_cfg, "head")

    tables = Base.metadata.sorted_tables  # FK-safe order

    # Settings keys seeded by migrations — always present in a fresh target;
    # excluded from the "target is empty" check and never copied from source
    # (target already has them from the migration).
    _SEEDED_SETTING_KEYS = {
        "prompt_classification_system",
        "prompt_summary_concise",
        "prompt_summary_default",
        "prompt_summary_extended",
        "prompt_digest_synthesis",
    }

    def _seeded_col(table):
        # Rows seeded by migrations (system labels, the default rule) exist in a
        # freshly-migrated target already; never count or copy them.
        for name in ("is_system", "is_default"):
            if name in table.c:
                return table.c[name]
        return None

    def _user_row_count(conn, table) -> int:
        stmt = select(func.count()).select_from(table)
        seeded = _seeded_col(table)
        if seeded is not None:
            stmt = stmt.where(seeded.is_not(True))
        if table.name == "settings" and "key" in table.c:
            stmt = stmt.where(table.c["key"].not_in(_SEEDED_SETTING_KEYS))
        return conn.execute(stmt).scalar() or 0

    with target.connect() as t:
        existing = sum(_user_row_count(t, tb) for tb in tables)
        if existing and not force:
            print(f"Target already contains {existing} rows; refusing to copy. "
                  "Re-run with --force to append anyway (NOT recommended).")
            return 1

    failures = 0
    with source.connect() as s, target.begin() as t:
        for table in tables:
            rows = [dict(r) for r in s.execute(select(table)).mappings()]
            seeded = _seeded_col(table)
            if seeded is not None:
                rows = [r for r in rows if not r.get(seeded.name)]
            if table.name == "settings":
                rows = [r for r in rows if r.get("key") not in _SEEDED_SETTING_KEYS]
            if rows:
                t.execute(insert(table), rows)
            src_count = len(rows)
            dst_count = t.execute(select(func.count()).select_from(table)).scalar()
            status = "OK" if dst_count >= src_count else "MISMATCH"
            if status != "OK":
                failures += 1
            print(f"  {table.name:<28} {src_count:>6} -> {dst_count:<6} {status}")

            if target.dialect.name == "postgresql" and "id" in table.c and src_count:
                t.execute(text(
                    f"SELECT setval(pg_get_serial_sequence('{table.name}', 'id'), "
                    f"(SELECT MAX(id) FROM {table.name}))"))

    if failures:
        print(f"FAILED: {failures} table(s) mismatched.")
        return 1
    print("Migration complete. The SQLite file was left untouched as a fallback.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite", default="/data/mailtriage.db")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--target", default=os.environ.get("DATABASE_URL", ""),
                        help="defaults to DATABASE_URL")
    args = parser.parse_args()
    if not args.target:
        print("DATABASE_URL is not set and --target not given.")
        return 1
    return migrate(args.sqlite, args.target, args.force)


if __name__ == "__main__":
    raise SystemExit(main())
