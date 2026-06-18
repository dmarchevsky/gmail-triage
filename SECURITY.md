# Security checklist (spec §6)

Status: all items implemented and covered by tests where noted.

## 1. OAuth scope minimization — ✅
- `gmail.modify` is the only Gmail scope requested (`app/services/gmail.py::SCOPES`);
  the auth URL defaults to exactly this scope (tests:
  `test_oauth_flow_stores_encrypted_token`, `test_build_auth_url_default_scope_modify_only`).
  In push (Pub/Sub) ingestion mode the **non-send-capable** `pubsub` scope is
  additionally requested so the same user token can pull notifications
  (`PUBSUB_SCOPE`; tests: `test_build_auth_url_push_includes_pubsub_scope`,
  `test_pubsub_scope_is_not_send_capable`). No service-account key is stored.
- `assert_scopes_safe()` rejects tokens carrying `gmail.send`,
  `gmail.compose`, or `mail.google.com` at token save **and** at app startup
  (tests: `test_send_capable_scope_rejected`, `test_startup_scope_guard`).
- The Gmail wrapper is an endpoint allowlist; there is no generic
  passthrough. Send/draft/insert/import/permanent-delete endpoints do not
  appear anywhere in `app/` (automated grep test:
  `test_no_send_capable_code_paths`).

## 2. Secrets at rest — ✅
- Gmail token, Telegram bot token and the OAuth client JSON are
  Fernet-encrypted with a key derived from `APP_SECRET_KEY`
  (test: `test_secret_setting_encrypted_at_rest`,
  `test_oauth_flow_stores_encrypted_token`).
- The app refuses to start with a missing/default `APP_SECRET_KEY` or
  `UI_PASSWORD` (test: `test_refuses_default_secrets`).
- `GET /settings` returns only `*_configured` markers, never secret values
  (tests: `test_settings_export_never_contains_secrets`,
  `test_gmail_auth_endpoint_never_returns_token`). Audit log payloads
  exclude secret values (`test_audit_log_never_contains_secret_values`).

## 3. Egress surface — ✅
- Documented in README §6: Google OAuth/Gmail, `api.telegram.org`, and the
  configured LLM endpoint. Push ingestion mode additionally contacts
  `pubsub.googleapis.com` — **outbound pull only**, so push adds no inbound
  endpoint and keeps the trusted-LAN model (no public HTTPS webhook to secure).
  Nothing else is contacted. `httpx` honors `HTTPS_PROXY` for users who route
  egress through a proxy.

## 4. UI/API auth — ✅
- Mandatory password; no auth-less mode (startup refusal).
- Session cookie `HttpOnly` + `SameSite=Lax`; HTTP Basic fallback for API
  use; login rate-limited (5/min; test: `test_login_rate_limited`).
- All `/api` endpoints require auth except `/status` (docker healthcheck),
  which leaks no configuration (test: `test_status_endpoint_is_minimal`).
- TLS is intentionally out of scope; use a reverse proxy (README §6).

## 5. Prompt-injection containment — ✅
- The LLM's only output channels are schema-constrained JSON
  (category/confidence/rationale, criteria revisions) or digest text; the
  model has no tools and cannot trigger actions.
- All system prompts instruct the model to treat email content as untrusted
  and ignore embedded instructions (`backend/app/prompts/*`).
- Actions derive exclusively from the rule engine over the schema-validated
  category — never from free text (closed action enum, validated at the API
  boundary; test: `test_action_enum_closed_set`).
- Digest text is HTML-escaped before Telegram
  (`test_live_send_and_watermark_no_email_twice` asserts escaping); the
  React UI renders rationale/digest text as text nodes (no
  `dangerouslySetInnerHTML` anywhere).

## 6. No body retention by default — ✅
- Bodies are fetched on demand; only a SHA-256 hash and Gmail snippet are
  stored unless `store_bodies` is enabled (test:
  `test_classify_pending_happy_path` asserts `body_text is None`).
- Log snippets are capped at 200 chars (`test_log_snippet_truncation`).

## 7. Container hardening — ✅
- Non-root user (`mailtriage`, uid 1000), `read_only: true` root FS with
  `tmpfs /tmp`, named volume for `/data`, healthcheck on `/api/v1/status`,
  `restart: unless-stopped` (see `Dockerfile`, `docker-compose.yml`).
