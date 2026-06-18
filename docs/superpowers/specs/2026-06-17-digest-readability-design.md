# Digest readability (Telegram formatting) — design

Date: 2026-06-17

Implements §5 ("Digest readability via Telegram formatting") of
`2026-06-17-triage-ui-tweaks-design.md`, which was proposal-only. Scope is the
**core set** of formatting improvements, always-on, with a **tag-safe message
splitter** so rich formatting survives Telegram's 4096-char message split.

## Goal

Make Telegram digests easier to scan: a clear header, the AI summary visually
set apart, a separator, and richer per-email lines — without ever emitting
broken HTML when a long digest is split into multiple messages.

## Decisions (from brainstorming)

- **Scope:** core set only. No category grouping; no synthesis-prompt changes.
- **Split safety:** make `split_message` tag-aware (close + reopen open tags
  across parts), not a fit-only fallback.
- **Config:** always-on. No new `Digest` column, no migration, no UI change.
  The existing `include_metadata` / `include_links` toggles still apply.

## 1. Message rendering — `app/services/digests.py`

The summary remains `escape_html`'d (the local LLM cannot emit HTML); all
formatting is structural around it.

`_render_message(digest, emails, summary, dry_run_prefix)` produces, joined by
`\n\n`:

1. `[DRY RUN]` — unchanged, only when `dry_run_prefix` is true.
2. Header (two lines):
   - `📬 <b>{esc(name)}</b> · {date}` where `{date}` is `datetime.now(tz)`
     formatted `%b %d` (tz = the digest's timezone, already resolved in this
     function; fall back to UTC as today).
   - `<b>{len(emails)}</b> new email(s)`
3. Summary: `<blockquote>{esc(summary)}</blockquote>`.
4. Per-email list (only when `digest.include_metadata`): a leading separator
   line `──────────` (ten U+2500), then one entry per email:
   - `🔹 <b>{esc(sender or '?')}</b> <i>{HH:MM}</i>`
   - on the next line, the subject: `<a href="{deep link}">{esc(subject)}</a>`
     when `digest.include_links`, otherwise just `{esc(subject)}`
     (subject falls back to `(no subject)`).

When `include_metadata` is false, the message is header + summary only (no
separator, no list) — matching today's behavior minus the list.

No-news message in `run_digest` gains a ✅:
`✅ <b>{esc(digest.name)}</b>: no news.` (was the same without the emoji).

`GMAIL_DEEP_LINK` and the `escape_html`/`tz` handling are unchanged.

## 2. Tag-safe splitter — `app/services/telegram.py` `split_message`

**Problem.** Current `split_message` splits at `\n` near the budget but falls
back to a hard mid-line cut (`cut = prefix_budget`) when no newline is near.
With a `<blockquote>…</blockquote>` wrapping a long summary, a cut inside it
yields one part with an unclosed `<blockquote>` and another with an orphan
`</blockquote>`. Telegram rejects each (HTTP 400), and the retry path drops
`parse_mode`, so the part is resent as plain text with literal tags visible.

**Fix — close + reopen open tags at every chunk boundary:**

- Split only at line (`\n`) boundaries; never cut mid-line. If a *single* line
  alone exceeds the budget, cut it at a character offset that is **not inside a
  `<…>` tag** (scan for `<`/`>` and back off to the last safe offset).
- While accumulating lines into a chunk, maintain a stack of currently-open
  tags, storing each tag's **full opening string** (e.g. `<blockquote>`,
  `<a href="https://…">`) so attributes are preserved on reopen. Parse a line's
  tags left-to-right: an opener `<name …>` pushes; a closer `</name>` pops the
  matching opener; self-closing / void tags are not expected in our content.
  (Telegram-allowed tags only: `b i u s code pre blockquote a span tg-spoiler`.)
- When a chunk is finalized with tags still open: append the matching closers
  `</name>` in reverse-stack order to that chunk, and **prepend** the stored
  opening strings (in stack order) to the next chunk. Every emitted part is then
  balanced, valid HTML.
- Preserve the `[i/n]` numbering prefix. Reserve headroom in the per-chunk
  budget (~64 chars) for the prefix plus reopened-tag overhead so a reopened
  tag never pushes a part back over the limit.
- The `len(text) <= limit` fast path (return `[text]`) is unchanged.

This is a general fix (not reliant on summaries staying short) and future-proofs
a later "expandable blockquote for the per-email list" enhancement.

### Decomposition

Keep `split_message` as the public entry point. Extract a small private helper
for tag bookkeeping so the logic is testable and `split_message` stays readable,
e.g.:

- `_scan_tags(line, stack)` — update the open-tag stack for one line, returning
  the updated stack (or mutate in place); pure and unit-testable.
- `_close_open(stack) -> str` / `_reopen(stack) -> str` — closers / openers
  strings for the current stack.

## 3. Testing

**`split_message` (unit, pure):**
- `len <= limit` → returns `[text]` unchanged.
- Long plain text with newlines → splits at line boundaries; parts carry
  `[i/n]`; rejoined visible text round-trips.
- Long text containing `<blockquote>…</blockquote>` spanning the split point →
  **each part is balanced HTML** (no unclosed/orphan tag); concatenated visible
  text (tags stripped) equals the original visible text.
- Inline tags (`<b>`, `<a href>`) within a single line are never broken.
- A single line longer than the budget is cut outside any `<…>` tag.

**`_render_message` (in `tests/test_m5_digests.py`):**
- Header contains `📬` and `<b>{name}</b>`; count is bold.
- Summary is wrapped in `<blockquote>…</blockquote>`.
- Separator `──────────` present only when `include_metadata` is true.
- Subject is an `<a href>` when `include_links` true; plain escaped text when
  false.
- Assert structural markers, not the literal date (date varies by run).

**Gates:** backend `ruff` clean + full `pytest` green. No migration. No frontend
change.

## Out of scope

Category grouping; synthesis-prompt edits; new settings/toggles/migrations;
Digests UI changes; the `<blockquote expandable>` variant (left as a documented
future enhancement that the tag-safe splitter now supports).
