# Gmail Auth-Failure Telegram Alert + Remote Reconnect Link

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When `poller_loop()` hits `GmailAuthError` (revoked/expired refresh token,
no client secret configured, or a 401 from the Gmail API), send a Telegram alert
immediately, then at most once per 24h while it stays broken, and stop once the
connection recovers. The alert includes a reconnect link built from a new
`public_base_url` setting (the user's Tailscale hostname), so reconnecting Gmail
is possible from any device on the tailnet, not just the machine running
mailtriage — without exposing the service to the public internet.

**Architecture:** One new nullable `gmail_auth.last_auth_alert_at` column
(persists across restarts, unlike `app_state`) drives the cooldown. A private
helper `_maybe_send_auth_alert()` in `poller.py` sends via the existing
`telegram.send_message()` (plain text URL, no inline keyboard needed) and is
called from `poller_loop()`'s `except GmailAuthError` branch; `poll_once()`'s
success path clears the marker on recovery. A new `public_base_url` setting
(plain string, `settings_service.DEFAULTS`) supplies the link text, editable
from the Notifications tab in Settings.tsx next to the existing Telegram
fields.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.0, Alembic, httpx, pytest +
respx; React + TypeScript, Vite.

## Global Constraints

- Ruff: `cd backend && .venv/bin/ruff check .` — must pass clean before every commit.
- Tests: `cd backend && .venv/bin/python -m pytest -q` — 0 failures.
- Frontend: `cd frontend && npm run lint && npm run build` — 0 errors.
- Never import app models inside Alembic migration functions; use `sa.table()` / `sa.column()` for DML (not needed here — purely additive nullable column, no DML).
- Background-task code (anything called from `poller_loop()`) must never raise — wrap in broad `except Exception` per CLAUDE.md.
- `public_base_url` is NOT a secret — do not add it to `settings_service.SECRET_KEYS`.
- No inline Telegram keyboard / `reply_markup` — a plain URL in the message text is sufficient (Telegram auto-links it) and `telegram.send_message()` is not being changed.
- Cooldown window is exactly `timedelta(hours=24)`, comparison is `now - last_auth_alert_at < AUTH_ALERT_COOLDOWN` (not `<=`).
- `last_auth_alert_at` must only be set *after* `telegram.send_message()` succeeds — if sending raises, leave it unset so the next poll cycle retries immediately rather than silently waiting out a cooldown that never fired.

---

### Task 1: Backend — migration, model column, setting, alert logic, poller hooks, tests

**Files:**
- Create: `backend/alembic/versions/<generated>_gmail_auth_alert_timestamp.py`
- Modify: `backend/app/models.py` — add `last_auth_alert_at` to `GmailAuth`
- Modify: `backend/app/services/settings_service.py` — add `public_base_url` to `DEFAULTS`
- Modify: `backend/app/services/poller.py` — add alert logic + two hook points
- Modify: `backend/tests/test_m1_gmail.py` — new tests

**Interfaces:**
- Consumes: existing `GmailAuthError` (`backend/app/services/gmail.py`), `telegram.send_message()` (`backend/app/services/telegram.py:124`), `settings_service.get_setting()`/`set_setting()`
- Produces: `GmailAuth.last_auth_alert_at: datetime | None`; setting key `public_base_url` (default `""`); `poller._maybe_send_auth_alert(session, error) -> None`; `poller._build_auth_alert_message(error, base_url) -> str`

- [ ] **Step 1: Create the Alembic migration**

Generate the real revision id — do not hand-pick one:
```bash
cd backend && .venv/bin/alembic revision -m "gmail auth alert timestamp"
```
This creates a file under `backend/alembic/versions/`. Edit it to match:
```python
"""Add gmail_auth.last_auth_alert_at for Telegram re-auth alert cooldown

Revision ID: <generated>
Revises: c2d3e4f5a6b7

Tracks when the last "Gmail needs reconnecting" Telegram alert was sent, so
poller_loop can alert immediately on first failure, then at most once per 24h
while broken, and clear it once poll_once() succeeds again. Nullable; NULL
means no alert is currently outstanding.
"""
import sqlalchemy as sa

from alembic import op

revision = '<generated>'
down_revision = 'c2d3e4f5a6b7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('gmail_auth',
                  sa.Column('last_auth_alert_at', sa.DateTime(timezone=True),
                            nullable=True))


def downgrade() -> None:
    op.drop_column('gmail_auth', 'last_auth_alert_at')
```

Verify it applies cleanly against a throwaway SQLite DB:
```bash
DATABASE_URL=sqlite:////tmp/test_migrate_authalert.db .venv/bin/alembic upgrade head
```
Expected: all migrations apply, no errors. Then `rm -f /tmp/test_migrate_authalert.db`.

- [ ] **Step 2: Add the model column**

In `backend/app/models.py`, `GmailAuth` class, add after `watch_expiration` (currently line 70):
```python
    # Last time a Telegram "reconnect Gmail" alert was sent for the current
    # auth-error streak. NULL = no alert outstanding (never failed, or the
    # connection recovered and poll_once() cleared it). Set by poller.py.
    last_auth_alert_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
```

- [ ] **Step 3: Add the setting default**

In `backend/app/services/settings_service.py`, add to `DEFAULTS` right after `"telegram_default_chat_id": ""` (currently line 102):
```python
    # Reachable base URL (e.g. Tailscale https://host.tailnet.ts.net:8080) used
    # only to build the Telegram "reconnect Gmail" link when auth breaks while
    # you're away from the LAN. Optional; falls back to instructional text when unset.
    "public_base_url": "",
```
Do NOT add it to `SECRET_KEYS` — it's not sensitive.

- [ ] **Step 4: Add the alert logic to `poller.py`**

Add `telegram` to the existing service imports at the top of `backend/app/services/poller.py` (it currently imports `gmail` and `settings_service` from `app.services` — extend that import line to include `telegram`).

Add near the top of the file (module-level constant, alongside other constants like `PUSH_FALLBACK_POLL_SECONDS`):
```python
AUTH_ALERT_COOLDOWN = timedelta(hours=24)
```
(`timedelta` is already imported in this file — reuse it, do not re-import.)

Add these two functions (placed near `_record_poll_failure`, matching its private-helper style):
```python
def _build_auth_alert_message(error: str, base_url: str | None) -> str:
    lines = [
        "⚠️ <b>MailTriage: Gmail reconnect needed</b>",
        f"Polling is failing: <code>{telegram.escape_html(error[:300])}</code>",
        "This will keep failing every poll cycle until you reconnect Gmail.",
    ]
    if base_url:
        lines.append(f"Reconnect: {base_url.rstrip('/')}/#/settings?tab=mailbox")
    else:
        lines.append(
            "Open MailTriage → Settings → Mailbox and tap Reconnect. (Set a"
            " \"Public base URL\" in Settings → Notifications to get a direct"
            " link here when you're away from the LAN.)"
        )
    lines.append("You'll get a reminder once every 24h until this is resolved.")
    return "\n".join(lines)


async def _maybe_send_auth_alert(session: Session, error: str) -> None:
    """Telegram-alert about a Gmail auth failure: immediately the first time,
    then at most once per AUTH_ALERT_COOLDOWN while it stays broken. Must never
    raise — called from poller_loop's `except GmailAuthError` clause, which has
    no outer handler of its own."""
    try:
        loaded = gmail.load_token(session)
        if loaded is None:
            return  # nothing to persist the cooldown against
        row, _token = loaded
        token = settings_service.get_setting(session, "telegram_bot_token")
        chat_id = settings_service.get_setting(session, "telegram_default_chat_id")
        if not token or not chat_id:
            return  # Telegram not configured — nothing to send
        now = datetime.now(UTC)
        if row.last_auth_alert_at is not None \
                and now - row.last_auth_alert_at < AUTH_ALERT_COOLDOWN:
            return  # still within the 24h cooldown
        base_url = settings_service.get_setting(session, "public_base_url")
        message = _build_auth_alert_message(error, base_url)
        await telegram.send_message(token, str(chat_id), message)
        row.last_auth_alert_at = now
        session.commit()
    except Exception as e:  # noqa: BLE001 — alerting must never crash the poller
        session.rollback()
        log.warning("gmail_auth_alert_failed", error=str(e))
```

Check the exact return type of `gmail.load_token()` before writing `row, _token = loaded` —
confirm in `backend/app/services/gmail.py` (`load_token`, ~line 153-175) whether it returns a
`(GmailAuth, dict)` tuple or something else, and adjust the unpacking to match reality rather
than assuming.

- [ ] **Step 5: Wire the two hook points in `poller_loop()` / `poll_once()`**

In `poller_loop()`'s `except GmailAuthError` branch (currently lines 306-310), add the alert call after `_record_poll_failure`:
```python
        except GmailAuthError as e:
            app_state.gmail_status = "auth_error"
            app_state.poller_last_error = str(e)
            log.warning("poll_auth_error", error=str(e))
            _record_poll_failure(session, str(e), kind="auth")
            await _maybe_send_auth_alert(session, str(e))
```

In `poll_once()`, where `app_state.gmail_status = "ok"` is set on success (currently lines 205-209), clear a stale marker:
```python
    app_state.gmail_status = "ok"
    app_state.gmail_email = client.auth_row.email_address
    reset_alert = client.auth_row.last_auth_alert_at is not None
    if reset_alert:
        client.auth_row.last_auth_alert_at = None
    if new_count:
        audit(session, "system", "poll_completed", {"mode": mode, "new_emails": new_count})
    if reset_alert or new_count:
        session.commit()

    return {"mode": mode, "new_emails": new_count}
```
This replaces the existing `if new_count: ... session.commit()` block — keep the conditional-commit structure, just add the `reset_alert` branch alongside it. Do not add an unconditional commit on every successful poll cycle.

Do NOT touch `pubsub.py`'s own `except gmail.GmailAuthError` branch — `poller_loop` already calls `poll_once()` every cycle regardless of ingest mode, so the same failure surfaces there too within at most 15 minutes; a second alert path would be redundant.

- [ ] **Step 6: Tests — `backend/tests/test_m1_gmail.py`**

Follow the existing `connected` fixture / `respx` conventions already in this file (e.g. `test_ensure_watch_renews_near_expiry`, which sets `connected.watch_expiration` directly to simulate time passing rather than mocking the clock — no `freezegun` dependency exists or is needed here). Check the top of the file for the existing Telegram-mock constant/helper pattern used in other test files (e.g. `test_m5_digests.py`'s `TG_SEND`/`tg_ok()`) and reuse the same style rather than inventing a new one.

Add:
```python
@respx.mock
async def test_auth_alert_sent_immediately_on_first_failure(connected, db_session):
    from app.services import poller, settings_service
    settings_service.set_setting(db_session, "telegram_bot_token", "TOKEN")
    settings_service.set_setting(db_session, "telegram_default_chat_id", "555")
    settings_service.set_setting(db_session, "public_base_url", "https://host.ts.net:8080")
    db_session.commit()
    tg = respx.post("https://api.telegram.org/botTOKEN/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 1}}))
    await poller._maybe_send_auth_alert(db_session, "Gmail API returned 401")
    assert tg.called
    sent = json.loads(tg.calls.last.request.content)
    assert "host.ts.net:8080/#/settings?tab=mailbox" in sent["text"]
    db_session.expire_all()
    assert connected.last_auth_alert_at is not None


@respx.mock
async def test_auth_alert_suppressed_within_cooldown(connected, db_session):
    from datetime import UTC, datetime, timedelta
    from app.services import poller, settings_service
    settings_service.set_setting(db_session, "telegram_bot_token", "TOKEN")
    settings_service.set_setting(db_session, "telegram_default_chat_id", "555")
    connected.last_auth_alert_at = datetime.now(UTC) - timedelta(hours=1)
    db_session.commit()
    tg = respx.post("https://api.telegram.org/botTOKEN/sendMessage")
    await poller._maybe_send_auth_alert(db_session, "boom")
    assert not tg.called


@respx.mock
async def test_auth_alert_resent_after_cooldown_elapses(connected, db_session):
    from datetime import UTC, datetime, timedelta
    from app.services import poller, settings_service
    settings_service.set_setting(db_session, "telegram_bot_token", "TOKEN")
    settings_service.set_setting(db_session, "telegram_default_chat_id", "555")
    connected.last_auth_alert_at = datetime.now(UTC) - timedelta(hours=25)
    db_session.commit()
    tg = respx.post("https://api.telegram.org/botTOKEN/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 1}}))
    await poller._maybe_send_auth_alert(db_session, "boom")
    assert tg.called


async def test_auth_alert_noop_without_telegram_configured(connected, db_session):
    from app.services import poller
    await poller._maybe_send_auth_alert(db_session, "boom")  # must not raise
    db_session.expire_all()
    assert connected.last_auth_alert_at is None


@respx.mock
async def test_auth_alert_swallows_telegram_failure(connected, db_session):
    from app.services import poller, settings_service
    settings_service.set_setting(db_session, "telegram_bot_token", "TOKEN")
    settings_service.set_setting(db_session, "telegram_default_chat_id", "555")
    db_session.commit()
    respx.post("https://api.telegram.org/botTOKEN/sendMessage").respond(500, json={"ok": False})
    await poller._maybe_send_auth_alert(db_session, "boom")  # must not raise
    db_session.expire_all()
    assert connected.last_auth_alert_at is None  # not marked sent — retries next cycle


async def test_poll_once_clears_alert_marker_on_recovery(connected, db_session):
    """poll_once() success clears a stale alert marker so a future failure
    alerts immediately rather than waiting out the old cooldown."""
    from datetime import UTC, datetime, timedelta
    from app.services import gmail, poller
    connected.last_auth_alert_at = datetime.now(UTC) - timedelta(hours=1)
    db_session.commit()
    with respx.mock:
        respx.get(f"{gmail.GMAIL_API}/profile").respond(200, json={
            "emailAddress": "me@gmail.test", "historyId": "2000"})
        await poller.poll_once(db_session)
    db_session.expire_all()
    assert connected.last_auth_alert_at is None
```
Check `gmail.GMAIL_API` is the correct constant name and that `/profile` is the right endpoint
path used by the `connected` fixture's baseline-sync path — confirm against `gmail.py` and the
existing baseline-sync tests in this file, and adjust the mocked route if the real endpoint
path/response shape differs.

Also add one settings round-trip test (in whichever existing test file already covers
`PUT /settings` → `GET /settings`, e.g. search for an existing test asserting a non-secret
setting round-trips unredacted) verifying `public_base_url` behaves the same way.

- [ ] **Step 7: Run linter and tests**

```bash
cd backend
.venv/bin/ruff check .
.venv/bin/python -m pytest -q
```
Expected: ruff clean; full suite passes including the new tests above.

- [ ] **Step 8: Commit**

```bash
git add backend/alembic/versions/ backend/app/models.py \
        backend/app/services/settings_service.py backend/app/services/poller.py \
        backend/tests/test_m1_gmail.py
git commit -m "feat: Telegram alert on Gmail auth failure with remote reconnect link"
```

---

### Task 2: Frontend — `public_base_url` field in Settings

**Files:**
- Modify: `frontend/src/api.ts` — add `public_base_url` to the `Settings` interface
- Modify: `frontend/src/pages/Settings.tsx` — add the input field + wire it into the existing save handler

**Interfaces:**
- Consumes: setting key `public_base_url` from Task 1
- Produces: a text input in the Notifications tab; no new API endpoints

- [ ] **Step 1: `frontend/src/api.ts`**

In the `Settings` interface, add next to `telegram_default_chat_id` (currently ~line 199):
```typescript
  public_base_url: string;
```

- [ ] **Step 2: `frontend/src/pages/Settings.tsx`**

In the Notifications tab's `form-grid` (currently lines ~849-872), add a field right after the
"Default chat id" label:
```tsx
            <label className="span2">
              Public base URL (optional)
              <input
                placeholder="https://mailtriage-host.tailnet-name.ts.net:8080"
                value={
                  draft.public_base_url !== undefined
                    ? draft.public_base_url
                    : settings.public_base_url
                }
                onChange={(e) =>
                  setDraft({ ...draft, public_base_url: e.target.value })
                }
              />
              <span className="sub">
                Used to build a reconnect link in Telegram auth-error alerts
                (e.g. a Tailscale hostname). Leave blank for plain-text
                instructions instead.
              </span>
            </label>
```
Confirm the `span2`/`sub` classes are the correct existing conventions for a full-width
labeled input with helper text by checking how other fields in this file use them — adjust
if this form-grid uses a different pattern for helper text.

Extend the existing "Save Telegram settings" `onClick` handler (currently lines ~876-882) to
include the new field:
```tsx
              onClick={() => {
                const values: Record<string, unknown> = {};
                if (telegramToken) values.telegram_bot_token = telegramToken;
                if (draft.telegram_default_chat_id !== undefined)
                  values.telegram_default_chat_id = draft.telegram_default_chat_id;
                if (draft.public_base_url !== undefined)
                  values.public_base_url = draft.public_base_url.trim();
                saveValues(values).then(() => setTelegramToken(""));
              }}
```

- [ ] **Step 3: Run frontend lint and build**

```bash
cd frontend
npm run lint
npm run build
```
Expected: 0 TypeScript errors, successful production build.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/api.ts frontend/src/pages/Settings.tsx
git commit -m "feat: add public base URL setting for Telegram reconnect link"
```

---

## Verification

1. **Backend gates:** `cd backend && .venv/bin/ruff check . && .venv/bin/python -m pytest -q` — 0 failures.
2. **Frontend gates:** `cd frontend && npm run lint && npm run build` — 0 errors.
3. **Docker boot check on a throwaway project** (this change includes a migration — do NOT run against the live `mailtriage-pg`/`mailtriage-data` volumes):
   ```bash
   docker compose -p mailtriage-verify -f docker-compose.yml up -d --build
   docker exec $(docker compose -p mailtriage-verify ps -q mailtriage) \
     curl -fsS http://localhost:8080/api/v1/status
   docker compose -p mailtriage-verify logs --tail=25 mailtriage   # expect startup_complete
   docker compose -p mailtriage-verify down -v
   ```
   (Publishing a host port collides with the live stack on 8080 — use `docker exec` against the
   container directly, or an override file that remaps the port, per prior verification notes.)
4. **Manual/UI check:** in the throwaway stack, open Settings → Notifications, confirm the new
   "Public base URL" field renders, saves, and round-trips on reload.
5. **Behavioral check (optional, requires real Telegram config):** configure Telegram + a
   `public_base_url` against a disposable Gmail test setup, force a `GmailAuthError` (e.g. an
   invalid stored token), run one poll cycle, confirm exactly one Telegram message arrives with
   a working `.../#/settings?tab=mailbox` link, confirm no repeat on the next cycle within 24h,
   then simulate recovery and confirm the marker clears.
