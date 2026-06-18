# Triage UI tweaks — design

Date: 2026-06-17

A batch of four UI/behavior changes plus one proposal-only section. Scope is
deliberately small and surgical; no architectural change.

## 1. Classification errors in the Recent activity feed

**Problem.** Classification failures (LLM timeout, invalid LLM output,
Gmail-not-found, Gmail auth expired, stall give-up) currently only set
`email.error`, which is visible solely in the email detail modal. They never
reach the dashboard "Recent activity" feed, which is built from the `AuditLog`
table (`/stats` → `recent_activity`).

**Decision.** Log **every failure occurrence** (not only terminal ones). A
stubborn email retried across the attempt cap may therefore appear multiple
times — accepted, for maximum real-time visibility, mirroring how
`actions_failed` already behaves.

**Backend.**
- New audit event: `event_type="classification_failed"`, `actor="system"`,
  payload `{"email_id": int, "reason": str, "error": str}`.
- `reason` is a short stable code: `timeout`, `invalid_output`,
  `gmail_not_found`, `gmail_auth`, `stalled`.
- Add a small helper in `app/services/classifier.py`:
  `_audit_classification_failed(session, email, reason, error)` so the call is
  DRY across the failure sites.
- Emit at each existing failure point that sets `email.status = error`:
  - `classify_email` — `GmailNotFound` (`gmail_not_found`), `LLMInvalidOutput`
    (`invalid_output`), `LLMTimeout` (`timeout`).
  - `_process_next` — `GmailAuthError` (`gmail_auth`).
  - `_recover_stalled_emails` — stall give-up after the attempt cap (`stalled`).
- Both the background queue and `POST /classify/run-now` route through
  `classify_email`, so those three reasons are covered by one emit site.
- `audit()` only adds to the session; the existing commit in each caller
  persists it (no extra commit needed). For `_recover_stalled_emails`, the
  sweep's `session.commit()` already covers it.

**Frontend (`src/activity.ts`).**
- Add `classification_failed: "Classification failed"` to `EVENT_LABELS`.
- Add a formatter:
  `classification_failed: (p) => \`${emailRef(p)} failed: ${p.error ?? "unknown error"}\``.
- No change to `/stats` enrichment: the payload carries `email_id`, so the
  existing enricher resolves sender/subject automatically. The `system` actor
  already renders with neutral tone.

**Test.** Backend test: a `LLMTimeout` during classification produces an
`AuditLog` row with `event_type == "classification_failed"` and the email id in
its payload.

## 2. Merge "Today" + "Last 7 days" into one card

**Layout (approved):** one **Processed** card with two aligned rows —
`Today → stats.today.processed` and `Last 7 days → stats.week.processed`.

- **Executed counts removed.**
- **Planned (dry-run) counts also removed** from this card (confirmed). Dry-run
  visibility is preserved by the existing dry-run banner at the top of the
  dashboard and per-email in the Emails list.
- Replaces the two cards at `Dashboard.tsx:108-125`. The Engine card is
  unchanged; the merged card sits first, so the row now has two cards
  (Processed, Engine).
- Add a small `.card-rows` style (label on the left, value right-aligned) in
  `styles.css`. No new component needed — plain markup inside the existing
  `.card`. Works identically on desktop and mobile (cards already stack on
  mobile via the `.cards` grid).

## 3. Mobile: Categories as a table (no horizontal scroll)

**Problem.** On mobile (`@media (max-width: 720px)`) every `.table` collapses to
stacked cards. The Categories precision-table should instead stay a real table —
and fit the viewport **without a horizontal scrollbar**.

**Approach.**
- Opt `.precision-table` out of the stacked-card collapse: re-assert
  `display: table` / `table-row` / `table-cell`, show its `thead`, and disable
  the `data-label` pseudo-element prefixes — all scoped to `.precision-table`
  inside the existing `max-width: 720px` block.
- Make it fit ~328px (a 360px phone minus padding) with 5 columns:
  - Abbreviated headers on mobile. Each `<th>` carries a full label and a short
    label as two spans (`.th-full` / `.th-abbr`); CSS shows one per breakpoint
    (lint-safe, no CSS text-replacement hacks). Abbreviations: Category→`Cat.`,
    Classified (1d)→`1d`, Classified (7d)→`7d`, Flagged wrong (7d)→`Wrong`,
    Precision (7d)→`Prec.`
  - Compact font-size (~0.72rem) and reduced cell padding on mobile.
  - Category cell wraps (`white-space: normal`); the four numeric columns are
    right-aligned and `white-space: nowrap`.
- `.table-scroll` already sets `overflow-x: visible` on mobile, so no scroll
  container fights this.

This is CSS-only plus the header-span markup in `Dashboard.tsx`. The desktop
rendering is unchanged.

## 4. Emails Actions column → badges for everything

**Problem.** `Emails.tsx:521-525` renders the actions as joined text
(`Add label → Work, Archive`). Replace with badges on desktop and mobile.

**Decision (approved):** badges for **all** actions, with a **distinct style for
non-label actions**.

- `add_label` → `LabelPill` (the existing colored pill using the label's own
  `text_color`/`background_color`), same component the detail modal already uses.
- Non-label actions → the existing `Badge` component (visually distinct from the
  pill) with per-type tones:
  - Archive → `info`
  - Mark read → `neutral`
  - Trash → `error`
  - Remove label → `warn`, text `Remove: {label_name}`
- **Dedupe** by (action_type + label_name) so an email actioned by multiple
  rules does not show repeated identical badges.
- Render order: label pills first, then non-label badges (stable, readable).
- Empty → `—` (unchanged).
- Mobile: the same JSX renders inside the `data-label="Actions"` cell. Add
  `.emails-table td[data-label="Actions"]::before { display: none }` to the
  mobile block so the `Actions:` text prefix is suppressed (badges read fine on
  their own, matching the Status cell treatment).

No backend change — the email list already returns `actions` with
`action_type` and `action_params` (incl. `label_name`, `text_color`,
`background_color`).

## 5. Digest readability via Telegram formatting — PROPOSAL ONLY

Not implemented in this round. Recorded here as recommendations. Current message
(`digests.py:_render_message`) is: optional `[DRY RUN]` / `<b>name</b> — N
email(s)` / escaped summary / optional `• HH:MM sender — subject [open]` list.
HTML parse mode is already enabled.

1. **Scannable header** — lead with an emoji + bold name + date and bold the
   count: `📬 <b>{name}</b> · {date}` then `<b>{N}</b> new email(s)`.
2. **Blockquote the synthesis** — wrap the summary in `<blockquote>` (or
   `<blockquote expandable>` when long) to separate the AI summary from headers
   and the list. Safe: the summary is already `escape_html`'d.
3. **Visual separator** — Telegram has no `<hr>`; insert a unicode rule
   (`──────────`) between the summary and the per-email list.
4. **Expandable per-email list** — wrap the metadata list in
   `<blockquote expandable>` when there are many emails to keep the message
   compact but available.
5. **Stronger metadata lines** — bold sender, italic time, and make the subject
   itself the tappable link instead of a trailing "open":
   `🔹 <b>{sender}</b> <i>{HH:MM}</i>` then `<a href="…">{subject}</a>`.
6. **Group by category** when a digest spans multiple categories — a
   `<b>📂 {category}</b>` subheader per group adds structure (small change: pass
   category through `_render_message`).
7. **Friendlier empty state** — `✅ <b>{name}</b>: no news.`
8. **Risk to flag for any future implementation** — richer HTML raises the
   chance that `split_message` (telegram.py) cuts *inside* a `<blockquote>` or
   other tag, producing invalid HTML in a message part (it then silently drops
   formatting for that part via the 400 fallback). Mitigation: split on
   blank-line/paragraph boundaries and/or close+reopen block tags per part.

## Out of scope / non-goals

- No new backend settings or migrations (the `classification_failed` event uses
  the existing `AuditLog` table and JSON payload).
- No change to digest rendering code in this round (section 5 is advisory).
- No change to desktop rendering of the Categories table or the Emails table
  beyond the Actions column.

## Quality gates

Per `CLAUDE.md`: backend `ruff check` + `pytest`, frontend `npm run lint` +
`npm run build`, and the Docker boot check before the feature commit.
