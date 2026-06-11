"""Telegram Bot API delivery — sendMessage only (spec §2.1).

HTML parse mode with escaping done by callers via `escape_html`; messages are
split at 4096 chars into numbered parts; 3 retries with backoff.
"""

import asyncio
import html

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


def split_message(text: str, limit: int = MAX_MESSAGE_CHARS) -> list[str]:
    """Split into <=limit chunks at line boundaries where possible; multi-part
    messages get a numbered '[i/n] ' prefix (kept within the limit)."""
    if len(text) <= limit:
        return [text]
    prefix_budget = limit - 10  # room for "[xx/yy] "
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= prefix_budget:
            chunks.append(remaining)
            break
        cut = remaining.rfind("\n", 1, prefix_budget)
        if cut < prefix_budget // 2:
            cut = prefix_budget
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
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
