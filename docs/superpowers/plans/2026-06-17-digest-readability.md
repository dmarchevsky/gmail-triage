# Digest readability (Telegram formatting) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Telegram digests easier to scan (emoji header, blockquoted summary, separator, richer per-email lines) while guaranteeing valid HTML across Telegram's 4096-char message split.

**Architecture:** Two backend changes — (1) a tag-aware `split_message` in `telegram.py` that closes open HTML tags at a chunk boundary and reopens them in the next chunk; (2) a rewritten `_render_message` in `digests.py` emitting the new structure (summary stays HTML-escaped; formatting is structural around it). Always-on, no migration, no frontend change.

**Tech Stack:** Python 3.12, pytest (+respx), FastAPI/SQLAlchemy app. No frontend.

**Commit policy:** `CLAUDE.md` requires all gates green and explicit user confirmation before any commit. Tasks verify with tests but do **not** commit individually; a single feature commit happens in Task 3 after gates pass and the user confirms. The commit includes the spec `docs/superpowers/specs/2026-06-17-digest-readability-design.md` and this plan.

Spec: `docs/superpowers/specs/2026-06-17-digest-readability-design.md`

---

## File Structure

- `backend/app/services/telegram.py` — add `import re`, tag-tracking helpers (`_TAG_RE`, `_tag_name`, `_track_tags`, `_closers`, `_split_long_line`), and rewrite `split_message` (modify).
- `backend/app/services/digests.py` — rewrite `_render_message`; add ✅ to the no-news message in `run_digest` (modify).
- `backend/tests/test_m5_digests.py` — add `split_message` close/reopen test; update `test_render_message_uses_digest_timezone`; add rich-render tests (modify).

---

## Task 1: Tag-aware `split_message`

**Files:**
- Modify: `backend/app/services/telegram.py`
- Test: `backend/tests/test_m5_digests.py`

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/test_m5_digests.py` (in the telegram unit-tests section, after `test_split_message_4096_numbered_parts`):

```python
def test_split_message_keeps_short_html_intact():
    text = "<blockquote>hi</blockquote>"
    assert telegram.split_message(text) == [text]


def test_split_message_closes_and_reopens_open_tags():
    """A <blockquote> whose content overflows 4096 chars must not be cut into
    parts with unbalanced tags; the splitter closes it at a boundary and reopens
    it in the next part so every part is valid standalone HTML."""
    import re as _re

    inner = "\n".join(f"quoted line {i}" for i in range(600))  # > 4096 chars
    text = f"<blockquote>{inner}</blockquote>"
    parts = telegram.split_message(text)

    assert len(parts) > 1
    for p in parts:
        body = p.split("] ", 1)[1]  # strip the "[i/n] " prefix
        assert len(p) <= 4096
        # balanced + every part sits inside the quote
        assert body.count("<blockquote>") == body.count("</blockquote>") >= 1
    # visible text (tags + prefixes stripped) round-trips
    visible = "".join(
        _re.sub(r"</?blockquote>", "", p.split("] ", 1)[1]) for p in parts
    ).replace("\n", "")
    assert visible == inner.replace("\n", "")
```

- [ ] **Step 2: Run the new tests, verify they FAIL**

Run: `cd backend && .venv/bin/python -m pytest "tests/test_m5_digests.py::test_split_message_closes_and_reopens_open_tags" -q`
Expected: FAIL — current `split_message` produces parts with an unclosed/orphan `<blockquote>` (assertion on balanced counts fails).

- [ ] **Step 3: Implement the tag-aware splitter**

In `backend/app/services/telegram.py`, add `import re` to the imports at the top (alongside `import asyncio`, `import html`). Then replace the existing `split_message` function with the helpers + new implementation below (keep `MAX_MESSAGE_CHARS = 4096` as-is):

```python
_TAG_RE = re.compile(r"<(/?)([a-zA-Z][\w-]*)([^>]*?)(/?)>")


def _tag_name(opening_tag: str) -> str:
    m = _TAG_RE.match(opening_tag)
    return m.group(2).lower() if m else ""


def _track_tags(text: str, stack: list[str]) -> None:
    """Update `stack` (full opening-tag strings) for every tag in `text`:
    push openers, pop the nearest matching opener for each closer. Self-closing
    tags (`<br/>`) are ignored."""
    for m in _TAG_RE.finditer(text):
        closing, name, self_close = m.group(1), m.group(2).lower(), m.group(4)
        if self_close:
            continue
        if closing:
            for i in range(len(stack) - 1, -1, -1):
                if _tag_name(stack[i]) == name:
                    del stack[i]
                    break
        else:
            stack.append(m.group(0))


def _closers(stack: list[str]) -> str:
    return "".join(f"</{_tag_name(t)}>" for t in reversed(stack))


def _split_long_line(line: str, budget: int) -> list[str]:
    """Safety net: split a single line longer than `budget` without cutting
    inside a `<...>` tag. Normal (short) lines are returned unchanged."""
    if len(line) <= budget:
        return [line]
    out: list[str] = []
    s = line
    while len(s) > budget:
        cut = budget
        lt = s.rfind("<", 0, cut)
        gt = s.rfind(">", 0, cut)
        if lt > gt and lt > 0:          # `cut` lands inside a tag → back off
            cut = lt
        out.append(s[:cut])
        s = s[cut:]
    if s:
        out.append(s)
    return out


def split_message(text: str, limit: int = MAX_MESSAGE_CHARS) -> list[str]:
    """Split into <=limit chunks at line boundaries. HTML tags still open at a
    chunk boundary are closed at the end of that chunk and reopened at the start
    of the next, so every emitted part is valid standalone HTML. Multi-part
    messages get a numbered '[i/n] ' prefix (kept within the limit)."""
    if len(text) <= limit:
        return [text]

    budget = limit - 64        # headroom for "[i/n] " + close/reopen overhead

    chunks: list[str] = []
    stack: list[str] = []      # tags currently open (running)
    carry: list[str] = []      # tags to reopen at the start of the current chunk
    cur: list[str] = []        # lines accumulated in the current chunk
    cur_len = 0

    def start_chunk() -> None:
        nonlocal cur, cur_len
        cur = []
        cur_len = len("".join(carry))

    start_chunk()
    for raw_line in text.split("\n"):
        for seg in _split_long_line(raw_line, budget):
            add = len(seg) + (1 if cur else 0)        # +1 for the joining "\n"
            if cur and cur_len + add + len(_closers(stack)) > budget:
                chunks.append("".join(carry) + "\n".join(cur) + _closers(stack))
                carry = list(stack)
                start_chunk()
            cur.append(seg)
            cur_len += len(seg) + (1 if len(cur) > 1 else 0)
            _track_tags(seg, stack)
    if cur:
        chunks.append("".join(carry) + "\n".join(cur) + _closers(stack))

    n = len(chunks)
    return [f"[{i + 1}/{n}] {c}" for i, c in enumerate(chunks)]
```

Note on the `carry` closure: `start_chunk` reads `carry` (a free variable from `split_message`); it only reassigns `cur`/`cur_len` (hence `nonlocal cur, cur_len`). `carry` is reassigned in the loop body directly in `split_message`'s scope, so no `nonlocal` is needed for it.

- [ ] **Step 4: Run the telegram tests, verify they PASS**

Run: `cd backend && .venv/bin/python -m pytest "tests/test_m5_digests.py::test_split_message_short_passthrough" "tests/test_m5_digests.py::test_split_message_4096_numbered_parts" "tests/test_m5_digests.py::test_split_message_keeps_short_html_intact" "tests/test_m5_digests.py::test_split_message_closes_and_reopens_open_tags" -q`
Expected: 4 passed. (The pre-existing tagless tests still pass because no tags are open at any boundary, so no closers/reopeners are added.)

- [ ] **Step 5: Lint**

Run: `cd backend && .venv/bin/ruff check app/services/telegram.py tests/test_m5_digests.py`
Expected: clean.

---

## Task 2: Rich `_render_message` + friendlier no-news message

**Files:**
- Modify: `backend/app/services/digests.py`
- Test: `backend/tests/test_m5_digests.py`

- [ ] **Step 1: Update the existing tz test and add render tests**

In `backend/tests/test_m5_digests.py`, update `test_render_message_uses_digest_timezone` — the per-email time is now rendered inside `<i>…</i>` instead of after a `•`:

```python
def test_render_message_uses_digest_timezone():
    from app.services.digests import _render_message

    digest = Digest(name="d", timezone="America/Los_Angeles",
                    include_metadata=True, include_links=False)
    email = Email(gmail_message_id="z1", sender="a@x.com", subject="s",
                  received_at=datetime(2026, 6, 12, 18, 30, tzinfo=UTC))
    msg = _render_message(digest, [email], "summary", dry_run_prefix=False)
    assert "<i>11:30</i>" in msg          # 18:30 UTC == 11:30 PDT

    digest.timezone = "Not/AZone"
    msg = _render_message(digest, [email], "summary", dry_run_prefix=False)
    assert "<i>18:30</i>" in msg          # invalid tz falls back to UTC
```

Then add these new tests right after it:

```python
def test_render_message_rich_formatting():
    from app.services.digests import _render_message

    digest = Digest(name="News", timezone="UTC",
                    include_metadata=True, include_links=True)
    email = Email(gmail_message_id="m9", sender="a@x.com", subject="Hello",
                  received_at=datetime(2026, 6, 12, 9, 5, tzinfo=UTC))
    msg = _render_message(digest, [email], "the summary", dry_run_prefix=False)
    assert "📬 <b>News</b>" in msg
    assert "<b>1</b> new email(s)" in msg
    assert "<blockquote>the summary</blockquote>" in msg
    assert "──────────" in msg
    assert '<a href="https://mail.google.com/mail/u/0/#all/m9">Hello</a>' in msg


def test_render_message_no_links_plain_subject():
    from app.services.digests import _render_message

    digest = Digest(name="News", timezone="UTC",
                    include_metadata=True, include_links=False)
    email = Email(gmail_message_id="m9", sender="a@x.com", subject="Hello",
                  received_at=datetime(2026, 6, 12, 9, 5, tzinfo=UTC))
    msg = _render_message(digest, [email], "s", dry_run_prefix=False)
    assert "<a href" not in msg
    assert "Hello" in msg


def test_render_message_no_metadata_omits_list():
    from app.services.digests import _render_message

    digest = Digest(name="News", timezone="UTC",
                    include_metadata=False, include_links=True)
    email = Email(gmail_message_id="m9", sender="a@x.com", subject="Hello",
                  received_at=datetime(2026, 6, 12, 9, 5, tzinfo=UTC))
    msg = _render_message(digest, [email], "s", dry_run_prefix=False)
    assert "──────────" not in msg
    assert "<blockquote>s</blockquote>" in msg
```

- [ ] **Step 2: Run those tests, verify they FAIL**

Run: `cd backend && .venv/bin/python -m pytest "tests/test_m5_digests.py::test_render_message_rich_formatting" "tests/test_m5_digests.py::test_render_message_uses_digest_timezone" -q`
Expected: FAIL (current format has no `📬`/`<blockquote>`/`<i>…</i>`).

- [ ] **Step 3: Rewrite `_render_message` and the no-news message**

In `backend/app/services/digests.py`, replace the entire `_render_message` function with:

```python
def _render_message(digest: Digest, emails: list[Email], summary: str,
                    dry_run_prefix: bool) -> str:
    esc = telegram.escape_html
    try:
        tz = ZoneInfo(digest.timezone or "UTC")
    except (KeyError, ZoneInfoNotFoundError):
        tz = UTC
    parts = []
    if dry_run_prefix:
        parts.append("[DRY RUN]")
    date_str = datetime.now(tz).strftime("%b %d")
    parts.append(
        f"📬 <b>{esc(digest.name)}</b> · {date_str}\n"
        f"<b>{len(emails)}</b> new email(s)")
    parts.append(f"<blockquote>{esc(summary)}</blockquote>")
    if digest.include_metadata:
        lines = ["──────────"]
        for e in emails:
            when = e.received_at.astimezone(tz).strftime("%H:%M") \
                if e.received_at else "?"
            sender = esc(e.sender or "?")
            subject = esc(e.subject or "(no subject)")
            if digest.include_links:
                subject = (
                    f'<a href="{GMAIL_DEEP_LINK.format(msg_id=e.gmail_message_id)}">'
                    f"{subject}</a>")
            lines.append(f"🔹 <b>{sender}</b> <i>{when}</i>\n{subject}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)
```

Then, in `run_digest`, the no-news branch currently sends:

```python
                    await telegram.send_message(
                        token, str(chat_id),
                        f"<b>{telegram.escape_html(digest.name)}</b>: no news.",
                    )
```

Change that message string to add the ✅:

```python
                    await telegram.send_message(
                        token, str(chat_id),
                        f"✅ <b>{telegram.escape_html(digest.name)}</b>: no news.",
                    )
```

(`datetime`, `UTC`, `ZoneInfo`, `ZoneInfoNotFoundError`, `GMAIL_DEEP_LINK`, and `telegram` are all already imported in `digests.py` — no new imports.)

- [ ] **Step 4: Run the render tests + the full M5 suite, verify PASS**

Run: `cd backend && .venv/bin/python -m pytest tests/test_m5_digests.py -q`
Expected: all pass. (Existing send tests assert substrings that survive — `"Synthesized digest."`, `"Bonds &lt;down&gt;"`, `"mail.google.com"`, `"Bonds summary"` are all still present inside the new structure.)

- [ ] **Step 5: Lint**

Run: `cd backend && .venv/bin/ruff check app/services/digests.py tests/test_m5_digests.py`
Expected: clean.

---

## Task 3: Full gates + single feature commit

**Files:** none (verification + commit only)

- [ ] **Step 1: Backend gates (whole suite)**

Run: `cd backend && .venv/bin/ruff check . && .venv/bin/python -m pytest -q`
Expected: ruff clean, 0 failures.

- [ ] **Step 2: Docker build check (safe — no container start, no volume mutation)**

Run: `cd /home/dima/work/gmail-triage && docker compose build 2>&1 | tail -3`
Expected: `Image mailtriage:latest Built`. (No migration in this change, so a live boot is optional and is the user's call.)

- [ ] **Step 3: Commit (after explicit user confirmation)**

Per `CLAUDE.md`, confirm with the user first. Then:

```bash
git add backend/app/services/telegram.py backend/app/services/digests.py \
        backend/tests/test_m5_digests.py \
        docs/superpowers/specs/2026-06-17-digest-readability-design.md \
        docs/superpowers/plans/2026-06-17-digest-readability.md
git commit -m "$(cat <<'EOF'
Digests: richer Telegram formatting with tag-safe message splitting

- telegram.split_message now closes open HTML tags at a chunk boundary and
  reopens them in the next chunk, so multi-part messages are always valid HTML.
- _render_message: emoji+bold header with date, bold count, blockquoted summary,
  unicode separator, and per-email lines with bold sender / italic time /
  subject-as-link. Friendlier no-news message. Always-on; no migration.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## Self-review notes

- **Spec coverage:** Rendering §1 → Task 2 (header, blockquote, separator, per-email lines, no-news ✅). Splitter §2 → Task 1 (close/reopen, line-boundary split, long-line safety net, `[i/n]`). Testing §3 → tests in Tasks 1 & 2 (split passthrough/numbered/close-reopen; render header/blockquote/separator/link-on-off/no-metadata; tz test updated). Always-on/no-migration honored (no model or settings change).
- **Placeholder scan:** none — every code step has complete code.
- **Type/name consistency:** helper names (`_TAG_RE`, `_tag_name`, `_track_tags`, `_closers`, `_split_long_line`) are defined in Task 1 and used only there; `split_message` signature unchanged; `_render_message` signature unchanged. `GMAIL_DEEP_LINK` format string matches the test's expected href exactly.
- **No frontend change; no Alembic migration.**
