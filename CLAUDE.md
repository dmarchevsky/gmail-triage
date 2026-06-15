# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

MailTriage is a self-hosted LLM email-triage app for a single Gmail account: it polls the
inbox, classifies mail with a local LLM against plain-language criteria, applies rules
(label / read / archive / trash), and sends scheduled Telegram digests — all configured
through a built-in web UI.

## Quality gates (run before every commit)

### Backend

Backend tooling lives in `backend/.venv` (the system `python`/`pytest` do not have the
deps). Either `source backend/.venv/bin/activate` first, or prefix with `.venv/bin/`:

```bash
cd backend
.venv/bin/ruff check .          # linter — must be clean
.venv/bin/python -m pytest -q   # full suite — must be 0 failures
```

### Frontend
```bash
cd frontend
npm run lint          # tsc --noEmit — must be 0 errors
npm run build         # production build — must succeed
```

### Docker boot check

Before committing a complete feature, also rebuild and start the app to confirm it boots:

```bash
docker compose build && docker compose up -d
curl -fsS http://localhost:8080/api/v1/status        # expect HTTP 200
docker compose logs --tail=25 mailtriage             # expect "startup_complete"
```

### Commit & push workflow

1. All gates above must pass (backend, frontend, docker boot).
2. Commit and push **only after explicit user confirmation**.
3. After pushing, verify the CI build goes green (`gh run list` / `gh run watch`) — a red
   build blocks shipping. CI runs the backend and frontend gates on every push to `main`.

## Local development

```bash
# Backend (from backend/, inside .venv — see Quality gates)
.venv/bin/pip install -e '.[dev]'                                  # editable install + dev extras
.venv/bin/python -m uvicorn app.main:app --reload --port 8080
.venv/bin/python -m pytest tests/test_m2_classification.py::test_name -q   # run a single test

# Frontend (from frontend/)
npm install
npm run dev        # Vite dev server; proxies /api → http://localhost:8080
```

The app refuses to start without a repo-root `.env` containing non-default `APP_SECRET_KEY`
and `UI_PASSWORD` (enforced by `config.validate_secrets()`).

## Architecture

- **Backend layering**: `app/api/` (thin route modules) → `app/services/` (business logic)
  → `app/models.py` (SQLAlchemy 2.0 ORM) → `app/db.py` (engine). `app/main.py` wires the
  routers and the `lifespan` that runs migrations and launches the background tasks.
- **Data flow**: `poller_loop` (Gmail `historyId` sync, scope-filtered) ingests mail as
  `pending` → `queue_loop` classifies one email at a time via `classifier.classify_one()`
  (ignore-senders → hard rules → LLM) → the rules engine applies label/read/archive/trash
  actions (each `Rule` carries its own `dry_run` flag). Digests are generated and delivered
  to Telegram on an APScheduler cron schedule.
- **LLM**: `app/services/llm.py` uses the `openai` SDK against a **local OpenAI-compatible**
  endpoint (`LLM_BASE_URL`, default `:8081/v1`). No cloud LLM, no fallback. Classification
  is JSON-schema-constrained and semaphore-bounded (`llm_max_concurrency`, default 1). This
  is *not* the Anthropic SDK; the Gmail client is hand-rolled over `httpx` (no google-api
  client) with a send-incapable endpoint allowlist.

## Repository layout

```
backend/   FastAPI app (Python 3.12, SQLAlchemy, Alembic, asyncio)
frontend/  React + TypeScript SPA (Vite, no test framework)
Dockerfile multi-stage: node build → python runtime
```

## Backend conventions

- **Linter**: ruff with `E, F, I, UP, B` rules, line-length 100, target py312.
  - E741 fires on single-letter variable names (`l`, `O`, `I`) — avoid them.
  - Import order is enforced (`I`); always run `ruff check` before committing.
- **Configuration is split in two**:
  - **Env-only** (`app/config.py`, pydantic `BaseSettings` from `.env`): `APP_SECRET_KEY`,
    `UI_PASSWORD`, `DATABASE_URL`, `LLM_BASE_URL`, `LLM_MODEL`, `HOST`, `PORT`, `TZ` — only
    what must be known before the DB is available.
  - **DB-backed runtime settings** (`settings` table via `app/services/settings_service.py`,
    Fernet-encrypted for secrets): `poll_interval_seconds`, `poll_scope_labels`,
    `llm_classify_timeout_seconds`, `llm_max_concurrency`, `ignore_senders`, etc. Adding a
    user-tunable value means adding it to the service defaults, **not** `config.py`.
- **Tests**: pytest with `asyncio_mode = auto`. Run from `backend/`. All tests must pass.
  - Fixtures in `tests/conftest.py`: `client` (fresh tmp SQLite + reset `app_state` + cleared
    config cache, per test), `auth_client` (logged in), `db_session` (direct DB assertions).
  - Gmail/HTTP traffic is mocked with `respx`; tests never hit a real LLM or Gmail.
- **Migrations**: every schema change needs an Alembic migration in `backend/alembic/versions/`.
  - Use `sa.table()` / `sa.column()` clause elements for DML inside migrations — never import app models (they reflect the current schema, not the migration-time schema).
  - Safety: include a compensating `UPDATE` in `upgrade()` to fix any rows that would violate new constraints.
- **Exception ordering**: catch the most specific exception first. `APITimeoutError` is a subclass of `APIConnectionError` — always catch `APITimeoutError` before `APIConnectionError`.
- **Background tasks**: `queue_loop`, `stall_checker`, and `poller_loop` are asyncio tasks started in `lifespan`. They must never crash the process — wrap top-level iteration in broad `except Exception`.
- **Shared state**: `app_state` (`app/state.py`) is a module-level singleton that persists across requests and test invocations. Tests that assert on `app_state` fields must reset them explicitly to avoid cross-test pollution.

## Frontend conventions

- **Type-check is the only gate**: `npm run lint` runs `tsc --noEmit`. All exported symbols must be used; remove dead code before committing.
- **Build is the integration test**: `npm run build` (tsc + vite) is what CI runs — local `lint` passes but a dead import can still break the build.
- Badge tone union: `"ok" | "warn" | "error" | "neutral" | "dry" | "info"` — add new tones to both `components.tsx` and `styles.css` together.

## Key design decisions

- **Per-email queue** (`queue_loop`): emails flow `pending → processing → classified/actioned/error/skipped`. The queue is continuous — there is no batch size or done/total counter. `pending_emails` in `/api/v1/status` is the progress signal.
- **LLM failures**: `APITimeoutError` (successful connection, request timed out) → `LLMTimeout` → per-email `error` status, queue continues. `APIConnectionError` (no connection) → `LLMUnavailable` → email reset to `pending`, queue backs off 60 s.
- **Stall recovery**: `stall_checker` resets emails stuck in `processing` longer than `llm_classify_timeout + 30 s` back to `pending`.
- **Reclassify is async**: `POST /emails/{id}/reclassify` returns the email in `pending` status immediately; the queue handles re-classification. All previous actions are wiped.
- **System labels**: Important, Starred, Spam are seeded by migration `c7f3a9b2e541` with `is_system=True`. Tests that assert on user-created labels must filter `is_system=True` rows out.
- **Gmail scope**: only messages whose Gmail label IDs intersect `poll_scope_labels` (configurable) are ingested. `SENT`, `DRAFT`, `TRASH`, `SPAM`, `CHAT` are always excluded.

## Docker

```bash
docker compose build && docker compose up -d   # rebuild + restart
docker compose logs -f mailtriage              # tail app logs
```

Compose runs **Postgres** as the primary store (`mailtriage-pg` volume); the
`mailtriage-data` SQLite volume is legacy — a migration source / fallback only.

Never run mutating operations (migrations, data imports) against the live data volumes
(`mailtriage-pg`, `mailtriage-data`) without a backup.
