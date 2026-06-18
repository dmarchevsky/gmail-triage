"""Telegram Bot API delivery — sendMessage only (spec §2.1).

HTML parse mode with escaping done by callers via `escape_html`; messages are
split at 4096 chars into numbered parts; 3 retries with backoff.
"""

import asyncio
import html
import re

import httpx

from app.logging_setup import get_logger

log = get_logger(__name__)

TELEGRAM_API = "https://api.telegram.org"
MAX_MESSAGE_CHARS = 4096
RETRIES = 3


class TelegramError(Exception):
    pass


def escape_html(text: str) -> str:
    return html.escape(text, quote=False)


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

    # Headroom for the "[i/n] " prefix plus the close/reopen overhead at chunk
    # boundaries. 64 is ample because the only tags that can span a line break in
    # our messages are short block tags (e.g. <blockquote>, ~25 chars to close +
    # reopen); inline tags (<b>/<i>/<a href>) always open and close on one line,
    # so they are never open at a boundary. A long-attribute tag able to span
    # lines would need a larger reserve — revisit this if rendering changes.
    budget = limit - 64

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


async def send_message(token: str, chat_id: str, text: str,
                       parse_mode: str | None = "HTML") -> list[str]:
    """Send (possibly split) message; returns Telegram message ids.
    Raises TelegramError after RETRIES failures."""
    message_ids: list[str] = []
    async with httpx.AsyncClient(timeout=30) as client:
        for part in split_message(text):
            payload: dict = {"chat_id": chat_id, "text": part,
                             "disable_web_page_preview": True}
            if parse_mode:
                payload["parse_mode"] = parse_mode
            last_error = ""
            for attempt in range(RETRIES):
                try:
                    resp = await client.post(
                        f"{TELEGRAM_API}/bot{token}/sendMessage", json=payload)
                except httpx.TransportError as e:
                    last_error = str(e)
                    await asyncio.sleep(2 ** attempt)
                    continue
                if resp.status_code == 200 and resp.json().get("ok"):
                    message_ids.append(str(resp.json()["result"]["message_id"]))
                    break
                last_error = f"{resp.status_code} {resp.text[:200]}"
                if resp.status_code == 400 and parse_mode:
                    # Bad HTML entities — retry this part as plain text.
                    payload.pop("parse_mode", None)
                    parse_mode = None
                    continue
                await asyncio.sleep(2 ** attempt)
            else:
                raise TelegramError(f"sendMessage failed after retries: {last_error}")
    return message_ids


async def test_connection(token: str, chat_id: str) -> dict:
    try:
        ids = await send_message(token, chat_id,
                                 "MailTriage: test message ✓", parse_mode=None)
        return {"ok": True, "message_ids": ids}
    except (TelegramError, httpx.HTTPError) as e:
        return {"ok": False, "error": str(e)[:300]}
