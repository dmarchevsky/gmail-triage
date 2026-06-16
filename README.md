# MailTriage

Self-hosted LLM email triage for a single Gmail account. MailTriage polls your
inbox (and any other Gmail tabs you choose), classifies mail with a **local**
llama.cpp model against plain-language criteria you write, applies rules (label /
mark read / archive / trash), and sends scheduled Telegram digests. Everything
is configured through a built-in web UI. Each rule has its own **dry-run** flag
(on by default) so you can review what *would* happen before anything touches
your mailbox.

**Hard guarantees**

- **Read-and-organize only.** The Gmail client wrapper exposes an allowlist of
  endpoints; send/draft/insert/import are unrepresentable in code, the only
  OAuth scope requested is `gmail.modify` (which cannot send), and the app
  refuses to run if a stored token ever carries a send-capable scope.
- **No permanent deletion.** "Trash" uses `messages.trash` (Gmail auto-purges
  after ~30 days). The permanent-delete endpoint does not exist in the code.
- **Local-only LLM.** Email content goes only to the LLM endpoint you
  configure. No cloud LLM, no fallback. Telegram receives only digest text and
  optional subject/sender metadata.
- **Single user, mandatory auth.** No auth-less mode; secrets encrypted at
  rest (Fernet, key from `APP_SECRET_KEY`).

---

## 1. Quick start

Prerequisites: Docker + Docker Compose, a Google account, a running
OpenAI-compatible LLM server (llama.cpp recommended; see §4), optionally a
Telegram bot.

```bash
git clone <this repo> mailtriage && cd mailtriage
cp .env.example .env
# edit .env:
#   APP_SECRET_KEY=$(openssl rand -hex 32)
#   UI_PASSWORD=<choose a password>
#   POSTGRES_PASSWORD=$(openssl rand -hex 16)
#   LLM_BASE_URL=http://host.docker.internal:8081/v1   # your llama.cpp
docker compose up -d --build
```

Compose runs two services: the app and a bundled PostgreSQL (not exposed on
the host; `docker compose exec postgres psql -U mailtriage` to inspect).
Outside Docker (bare-metal dev), leave `DATABASE_URL` unset and the app uses
a local SQLite file under `./data` instead.

Open http://localhost:8080, log in with `UI_PASSWORD`, and follow the
first-run wizard: connect Gmail → test the LLM → create your first category.
Rules start in dry-run mode until you disable it per-rule.

## 2. Google Cloud OAuth app setup (one-time, ~5 min)

MailTriage needs OAuth client credentials so *you* authorize it against
*your* Gmail. Nothing is sent to anyone else's servers.

1. Go to https://console.cloud.google.com/ → create (or pick) a project,
   e.g. `mailtriage`.
2. **APIs & Services → Library** → search "Gmail API" → **Enable**.
3. **APIs & Services → OAuth consent screen**:
   - User type: **External** (fine for personal use) → Create.
   - App name `MailTriage`, your email for the contact fields → Save.
   - Scopes: you can skip adding scopes here (requested at runtime).
   - Test users: **add your own Gmail address**. (While the app is in
     "Testing" status only test users can authorize — that's exactly what we
     want; no verification process needed.)
4. **APIs & Services → Credentials → Create credentials → OAuth client ID**:
   - Application type: **Web application**.
   - Name: `mailtriage`.
   - Authorized redirect URIs: `http://localhost:8080/api/v1/gmail/oauth/callback`
     (adjust host/port if you serve the UI elsewhere — must match the URL you
     use in the browser).
   - Create → **Download JSON**.
5. In the MailTriage wizard (or Settings → Gmail), paste the JSON file's
   contents and click **Connect Gmail**. Google will warn the app is
   unverified ("Continue" under *Advanced*) — expected for a personal test
   app. The consent screen will show only the
   "Read, compose, and manage your email" `gmail.modify` permission
   (despite Google's wording, `gmail.modify` cannot send mail; MailTriage
   additionally asserts at startup that no send-capable scope was granted).

Refresh tokens for test-status apps are long-lived; if Google ever expires
one, the UI shows `auth_error` and you just click Connect again.

## 3. Telegram bot (optional, for digests)

1. Message **@BotFather** → `/newbot` → pick a name → copy the bot token.
2. Message your new bot once (bots can't initiate chats).
3. Get your chat id: message **@userinfobot**, or
   `curl https://api.telegram.org/bot<TOKEN>/getUpdates` after messaging the
   bot and read `message.chat.id`.
4. Settings → Telegram: paste token + chat id → **Send test message**.

## 4. LLM serving (llama.cpp)

**Recommended: run llama.cpp on the host**, especially with GPU/Vulkan
acceleration (e.g. AMD iGPU) — GPU passthrough into Docker is hardware
specific and not worth the trouble:

```bash
llama-server -m your-model.gguf --port 8081 --host 0.0.0.0 -c 8192
```

The default `LLM_BASE_URL=http://host.docker.internal:8081/v1` reaches the
host from the container (compose maps `host-gateway`). Any OpenAI-compatible
server works (Ollama, vLLM, …).

Alternatively, uncomment the bundled CPU `llama-cpp` service in
`docker-compose.yml`, drop a model into `./models/model.gguf`, and set
`LLM_BASE_URL=http://llama-cpp:8081/v1`.

**Model guidance:** classification at temperature 0 with JSON-schema
enforcement works acceptably from ~4B parameters; a 7–8B instruct model
(Qwen-class) is the recommended floor for reliable multi-category
criteria-following and digest quality. Context needs: classification ≤ ~4k
tokens/request; digest synthesis up to ~8k (`-c 8192`).

`scripts/llm_smoke.py` runs a live classification smoke test:
`LLM_BASE_URL=http://localhost:8081/v1 python backend/scripts/llm_smoke.py`.

## 5. Using MailTriage

### Categories

Write plain-language criteria; that text *is* the LLM prompt. The category
editor also lets you **quick-create a rule or a digest** for the category in
the same step — handy when setting up a new category from scratch.

Full edit history (criteria versions with diffs) is accessible from the
category row; any version can be restored.

### Labels

Labels are Gmail labels that rules apply to emails. They are managed
independently from categories on the **Labels** page. Predefined system
labels — **Important**, **Starred**, and **Spam** — are always available and
cannot be deleted. User labels support Gmail's slash-nested naming
(`MailTriage/Finance`) and custom colors. A label is created in Gmail the
first time a live (non-dry-run) rule uses it.

### Rules

Ordered matchers → actions. Each rule specifies:

- **Match:** category + min confidence, optional sender pattern (glob).
- **Actions:** `add_label`, `remove_label`, `mark_read`, `archive`, `trash`
  (one or more per rule).
- **Dry-run flag** (per-rule, default on): planned actions are recorded and
  shown in the UI but nothing in Gmail changes. Disabling dry-run never
  retro-executes previously planned actions.
- **Stop processing:** checked by default; first match wins. Uncheck to let
  lower-priority rules also fire.

A sender-pattern rule with *no* category is a **hard rule**: it classifies
deterministically with confidence 1.0 and bypasses the LLM entirely.

### Emails

The Emails page lists all processed mail with filterable columns (category,
status, confidence, free-text). Clicking a row opens the detail view with
classification rationale, the saved summary, planned/executed actions, and a
feedback form. From the detail view you can re-run classification + rules for a
single email.

Bulk operations (multi-select checkboxes): re-classify with LLM, or re-run
rules only. The selection bar offers **"Select all N matching emails"** to
extend a page selection across all pages without pagination limits.

### Mailbox scope

By default MailTriage polls your Inbox. Settings → **Poll scope** lets you
add Gmail's tabbed categories (Promotions, Social, Updates, Forums) so
newsletters or social notifications are triaged too — without having to move
them to the Inbox first.

### Summaries & prompts

When the LLM classifies an email, MailTriage also generates a plain-text
**summary** and stores it (shown in the email detail view and reused by
digests). Verbosity follows a system-wide **summarization depth** — *concise*,
*default*, or *extended* — set in Settings → LLM Processing; changing the depth
affects newly-classified mail only. Emails classified by a hard rule (sender
match, no LLM) have no summary.

The LLM prompts are editable in **Settings → LLM Prompts**: the classification
system prompt, the three summary-depth prompts, and the digest synthesis
prompt. Each defaults to the built-in text shipped with the app.

### Digests

Pick categories, send times (e.g. `07:00, 16:00`) + timezone, min
confidence, and optionally a per-digest Telegram chat id. Digests are built
from the per-email summaries saved at classification time, so no email is
re-summarized at digest time. The **digest mode** (Settings → LLM Processing)
chooses how: **assemble** (default) lists the saved summaries with no LLM call,
while **synthesize** makes a single LLM call to combine them into a cohesive
digest. No email is included twice by the same digest; failed sends keep emails
eligible for the next run. Example: *Market news — 07:00 & 16:00 — category
MarketNews — min confidence 0.8.*

### Feedback & criteria refinement

Flag a misclassified email (correct category + optional note). The LLM
proposes a criteria revision shown as a diff, consolidated with any other
pending feedback for the same category. Approve / edit / reject from the
**Feedback** page. Approval bumps the category's criteria version (full
history with restore). The Dashboard shows per-category empirical precision —
LLM confidence is *not calibrated*; tune rule thresholds against these
empirical counts.

## 6. Operations

### Backup & restore

State lives in PostgreSQL (`mailtriage-pg` volume); encrypted tokens are
rows in it. Keep `APP_SECRET_KEY` with the backup — encrypted tokens are
unreadable without it. `docker compose down && up` loses nothing.

```bash
# backup
docker compose exec postgres pg_dump -U mailtriage mailtriage | gzip \
    > mailtriage-pg-backup.sql.gz
# restore (into a fresh, empty database)
gunzip -c mailtriage-pg-backup.sql.gz | \
    docker compose exec -T postgres psql -U mailtriage mailtriage
```

For bare-metal SQLite mode, back up the `./data/mailtriage.db` file (or the
`mailtriage-data` volume) instead.

### Migrating from SQLite (pre-Postgres installs)

1. **Back up the SQLite file first:**
   ```bash
   docker run --rm -v mailtriage-data:/data -v "$PWD":/b alpine \
       cp /data/mailtriage.db /b/mailtriage-sqlite-backup.db
   ```
2. Add `POSTGRES_PASSWORD` to `.env`, rebuild, start Postgres, migrate:
   ```bash
   docker compose build
   docker compose up -d postgres
   docker compose run --rm mailtriage python scripts/migrate_sqlite_to_pg.py
   docker compose up -d
   ```
   The script creates the schema (Alembic), copies every table with ids
   preserved, resets Postgres sequences, and verifies per-table row counts.
   The SQLite file is left untouched in the `mailtriage-data` volume as a
   fallback.

### Egress surface

The container talks to exactly:

| Destination | Purpose |
|---|---|
| `accounts.google.com`, `oauth2.googleapis.com` | OAuth consent/token/revoke |
| `gmail.googleapis.com` | Gmail REST API |
| `api.telegram.org` | digest delivery (only if configured) |
| your `LLM_BASE_URL` | classification/summaries (usually LAN/localhost) |

To firewall the container, restrict egress to those hosts (or set
`HTTPS_PROXY` in the environment; httpx honors it).

### Reverse proxy / TLS

The app serves plain HTTP on :8080 and is meant for a trusted LAN. For
remote access put it behind a TLS reverse proxy (Caddy/Traefik/nginx) and
keep the UI password. Session cookies are `HttpOnly` + `SameSite=Lax`;
login is rate-limited.

### Troubleshooting

- **Gmail `auth_error`** — token revoked/expired: Settings → Gmail →
  Connect again. The poller keeps running and recovers automatically.
- **LLM `unreachable`** — poller keeps fetching; emails queue as `pending`
  and are classified automatically when the endpoint returns.
- **History expired (long downtime)** — the poller falls back to a
  time-based re-sync automatically; duplicates are impossible
  (unique message id).

## 7. Development

```bash
cd backend && uv venv .venv && uv pip install -p .venv/bin/python -e '.[dev]'
APP_SECRET_KEY=dev-secret UI_PASSWORD=dev .venv/bin/uvicorn app.main:app --port 8080 --reload
cd frontend && npm install && npm run dev    # Vite dev server proxies /api
.venv/bin/pytest                              # backend tests
.venv/bin/ruff check .                        # lint
```

Schema changes: edit `app/models.py`, then
`alembic revision --autogenerate -m "..."` (migrations run on startup).

## 8. Security checklist

See [SECURITY.md](SECURITY.md) for the full checklist tracked against the
spec (scope minimization, secrets at rest, prompt-injection containment,
container hardening, redaction audit).
