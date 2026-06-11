# MailTriage

Self-hosted LLM email triage for a single Gmail account. MailTriage polls your
inbox, classifies mail with a **local** llama.cpp model against plain-language
criteria you write, applies rules (label / mark read / archive / trash), and
sends scheduled Telegram digests. Everything is configured through a built-in
web UI. A global **dry-run** mode (on by default) lets you review what *would*
happen before anything touches your mailbox.

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
#   LLM_BASE_URL=http://host.docker.internal:8081/v1   # your llama.cpp
docker compose up -d --build
```

Open http://localhost:8080, log in with `UI_PASSWORD`, and follow the
first-run wizard: connect Gmail → test the LLM → create your first category.
Dry-run stays ON until you disable it from the sidebar.

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

1. **Categories** — write plain-language criteria; that text *is* the LLM
   prompt. Each category maps to a Gmail label (`MailTriage/<Name>` by
   default), auto-created when first used.
2. **Rules** — ordered matchers (category, min confidence, optional sender
   pattern) → actions (`add_label`, `remove_label`, `mark_read`, `archive`,
   `trash`). First match wins unless "stop processing" is off. A sender
   pattern rule with *no* category is a **hard rule**: it classifies
   deterministically with confidence 1.0 and bypasses the LLM. No rule
   matched → email is left untouched, always.
3. **Dry-run** — pipeline runs fully, planned actions are recorded and
   badged in the UI, nothing in Gmail changes, digests render in the UI
   instead of sending. Disabling dry-run never retro-executes old plans.
4. **Digests** — pick categories, times (e.g. `07:00, 16:00`) + timezone,
   min confidence, chat id. Two-stage summarization (per-email
   micro-summaries → synthesis). No email is ever summarized twice by the
   same digest; failed sends keep emails eligible for the next run.
   Example: *Market news — 07:00 & 16:00 — category MarketNews — min
   confidence 0.8.*
5. **Feedback** — flag a misclassified email (correct category + note); the
   LLM proposes a criteria revision shown as a diff; approve / edit / reject.
   Approval bumps the category's criteria version (full history with diffs
   and restore in the Categories page). Dashboard shows per-category
   empirical precision — LLM confidence is *not calibrated*; tune rule
   thresholds against these counts.

## 6. Operations

### Backup & restore

All state lives in the `mailtriage-data` volume (SQLite + encrypted tokens):

```bash
# backup
docker run --rm -v mailtriage-data:/data -v "$PWD":/backup alpine \
    tar czf /backup/mailtriage-backup.tgz -C /data .
# restore (container stopped)
docker run --rm -v mailtriage-data:/data -v "$PWD":/backup alpine \
    sh -c "rm -rf /data/* && tar xzf /backup/mailtriage-backup.tgz -C /data"
```

Keep `APP_SECRET_KEY` with the backup — encrypted tokens are unreadable
without it. `docker compose down && up` loses nothing (named volume).

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
