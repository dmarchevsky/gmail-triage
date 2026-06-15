# MailTriage — Claude Code guidance

## Quality gates (run before every commit)

### Backend
```bash
cd backend
ruff check .          # linter — must be clean
pytest -q             # full suite — must be 0 failures
```

### Frontend
```bash
cd frontend
npm run lint          # tsc --noEmit — must be 0 errors
npm run build         # production build — must succeed
```

CI runs both automatically on every push to `main`. A red build blocks shipping.

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
- **Tests**: pytest with `asyncio_mode = auto`. Run from `backend/`. All 128 tests must pass.
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

Never run mutating operations (migrations, data imports) against the live `mailtriage-data` volume without a backup.
