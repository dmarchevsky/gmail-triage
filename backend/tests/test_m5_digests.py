"""M5 acceptance: eligibility across runs (no email twice), message splitting,
failed send keeps emails eligible, dry-run renders only, scheduler triggers."""

import json
from datetime import UTC, datetime, timedelta

import pytest
import respx
from httpx import Response

from app.models import Category, Digest, DigestRun, DigestRunStatus, Email, EmailStatus
from app.services import telegram
from app.services.digest_scheduler import build_triggers, parse_hhmm
from tests.test_m2_classification import CHAT_URL, llm_response

TG_SEND = "https://api.telegram.org/bot123:abc/sendMessage"


def tg_ok(message_id=42):
    return Response(200, json={"ok": True, "result": {"message_id": message_id}})


# ── telegram unit tests ──────────────────────────────────────────────────────

def test_split_message_short_passthrough():
    assert telegram.split_message("hello") == ["hello"]


def test_split_message_4096_numbered_parts():
    text = "\n".join(f"line {i} " + "x" * 80 for i in range(200))
    parts = telegram.split_message(text)
    assert len(parts) > 1
    assert all(len(p) <= 4096 for p in parts)
    n = len(parts)
    for i, p in enumerate(parts):
        assert p.startswith(f"[{i + 1}/{n}] ")
    # nothing lost (strip the prefixes and whitespace introduced at joins)
    rejoined = "".join(p.split("] ", 1)[1] for p in parts).replace("\n", "")
    assert rejoined == text.replace("\n", "")


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


def test_escape_html():
    assert telegram.escape_html("<b>x & y</b>") == "&lt;b&gt;x &amp; y&lt;/b&gt;"


@respx.mock
@pytest.mark.anyio
async def test_send_message_retries_then_fails():
    route = respx.post(TG_SEND).respond(500, json={"ok": False})
    import asyncio
    real_sleep = asyncio.sleep

    async def fast_sleep(_s):
        await real_sleep(0)

    asyncio.sleep, saved = fast_sleep, asyncio.sleep  # type: ignore[assignment]
    try:
        with pytest.raises(telegram.TelegramError):
            await telegram.send_message("123:abc", "5", "hi", parse_mode=None)
    finally:
        asyncio.sleep = saved  # type: ignore[assignment]
    assert route.call_count == 3


# ── scheduler ────────────────────────────────────────────────────────────────

def test_parse_hhmm_and_triggers():
    assert parse_hhmm("07:00") == (7, 0)
    assert parse_hhmm("23:59") == (23, 59)
    with pytest.raises(ValueError):
        parse_hhmm("24:00")
    digest = Digest(name="d", cron_times=["07:00", "16:30"],
                    timezone="America/Los_Angeles")
    triggers = build_triggers(digest)
    assert len(triggers) == 2
    assert "America/Los_Angeles" in str(triggers[0].timezone)


# ── digest pipeline fixtures ─────────────────────────────────────────────────

@pytest.fixture()
def digest_setup(auth_client, db_session):
    """Category + 2 classified emails + a digest; telegram configured."""
    from app.services import settings_service

    settings_service.set_setting(db_session, "telegram_bot_token", "123:abc")
    settings_service.set_setting(db_session, "telegram_default_chat_id", "555")
    db_session.commit()

    cat = Category(name="MarketNews", criteria_md="m")
    db_session.add(cat)
    db_session.flush()
    now = datetime.now(UTC)
    e1 = Email(gmail_message_id="d1", sender="a@x.com", subject="S&P up",
               snippet="S&P rose 1%", summary="S&P summary", status="classified",
               classification_id=cat.id, confidence=0.9,
               received_at=now - timedelta(hours=2))
    e2 = Email(gmail_message_id="d2", sender="b@y.com", subject="Bonds <down>",
               snippet="Yields rose", summary="Bonds summary", status="classified",
               classification_id=cat.id, confidence=0.85,
               received_at=now - timedelta(hours=1))
    low = Email(gmail_message_id="d3", sender="c@z.com", subject="low conf",
                snippet="x", status="classified", classification_id=cat.id,
                confidence=0.3, received_at=now)
    db_session.add_all([e1, e2, low])
    db_session.commit()

    digest = auth_client.post("/api/v1/digests", json={
        "name": "Market news", "category_ids": [cat.id],
        "cron_times": ["07:00", "16:00"], "timezone": "UTC",
        "min_confidence": 0.8}).json()
    return {"digest": digest, "cat": cat.id,
            "e1": e1.id, "e2": e2.id, "low": low.id}


def mock_llm_text(text="Synth body."):
    return respx.post(CHAT_URL).mock(return_value=llm_response(text))


def _set_digest_mode(db_session, digest_id, mode):
    """Set mode on a specific digest; replaces the removed global setting."""
    from sqlalchemy import update  # noqa: PLC0415

    from app.models import Digest as DigestModel  # noqa: PLC0415
    db_session.execute(
        update(DigestModel).where(DigestModel.id == digest_id).values(mode=mode))
    db_session.commit()


# ── digest behavior ──────────────────────────────────────────────────────────

def test_digest_crud_validation(auth_client, digest_setup):
    bad = auth_client.post("/api/v1/digests", json={
        "name": "x", "cron_times": ["25:00"]})
    assert bad.status_code == 422
    bad_cat = auth_client.post("/api/v1/digests", json={
        "name": "x", "category_ids": [999]})
    assert bad_cat.status_code == 400
    listed = auth_client.get("/api/v1/digests").json()
    assert len(listed) == 1 and listed[0]["name"] == "Market news"


@respx.mock
def test_assemble_uses_saved_summaries_without_llm(auth_client, db_session, digest_setup):
    """Default assemble mode: the body is the saved per-email summaries, no LLM call."""
    chat = respx.post(CHAT_URL)
    tg = respx.post(TG_SEND)
    d = digest_setup["digest"]

    run = auth_client.post(f"/api/v1/digests/{d['id']}/run-now",
                           json={"preview": True}).json()
    assert run["status"] == "dry_run"
    assert sorted(run["email_ids"]) == sorted([digest_setup["e2"], digest_setup["e1"]])
    assert "S&P summary" in run["summary_text"]
    assert "Bonds summary" in run["summary_text"]
    assert tg.call_count == 0                       # nothing sent in preview
    assert chat.call_count == 0                     # assemble never calls the LLM

    # Preview must NOT consume eligibility: preview again, same emails eligible.
    run2 = auth_client.post(f"/api/v1/digests/{d['id']}/run-now",
                            json={"preview": True}).json()
    assert sorted(run2["email_ids"]) == sorted(run["email_ids"])


@respx.mock
def test_assemble_falls_back_to_snippet_when_no_summary(auth_client, db_session,
                                                        digest_setup):
    """An email without a saved summary falls back to its Gmail snippet."""
    db_session.add(Email(gmail_message_id="d9", sender="n@x.com", subject="no summary",
                         snippet="snippet fallback", status="classified",
                         classification_id=digest_setup["cat"], confidence=0.95,
                         received_at=datetime.now(UTC)))
    db_session.commit()
    d = digest_setup["digest"]
    run = auth_client.post(f"/api/v1/digests/{d['id']}/run-now",
                           json={"preview": True}).json()
    assert "snippet fallback" in run["summary_text"]


@respx.mock
def test_synthesize_mode_one_llm_call(auth_client, db_session, digest_setup):
    """Synthesize mode makes exactly one LLM call over the saved summaries."""
    d = digest_setup["digest"]
    _set_digest_mode(db_session, d["id"], "synthesize")
    chat = mock_llm_text("Synthesized digest.")
    tg = respx.post(TG_SEND).mock(return_value=tg_ok())

    run = auth_client.post(f"/api/v1/digests/{d['id']}/run-now").json()
    assert run["status"] == "success"
    assert run["summary_text"] == "Synthesized digest."
    assert chat.call_count == 1
    sent = json.loads(tg.calls[0].request.content)
    assert "Synthesized digest." in sent["text"]
    assert "S&P summary" in chat.calls[0].request.content.decode()


@respx.mock
def test_live_send_and_watermark_no_email_twice(auth_client, db_session, digest_setup):
    tg = respx.post(TG_SEND).mock(return_value=tg_ok())
    d = digest_setup["digest"]

    run = auth_client.post(f"/api/v1/digests/{d['id']}/run-now").json()
    assert run["status"] == "success"
    assert len(run["email_ids"]) == 2  # low-confidence excluded
    sent = json.loads(tg.calls[0].request.content)
    assert sent["parse_mode"] == "HTML"
    assert "Bonds &lt;down&gt;" in sent["text"]     # HTML-escaped subject
    assert "mail.google.com" in sent["text"]        # deep links on

    # Second run: nothing new -> empty, no email included twice.
    run2 = auth_client.post(f"/api/v1/digests/{d['id']}/run-now").json()
    assert run2["status"] == "empty"
    assert run2["email_ids"] == []

    # New email after watermark becomes eligible; old ones stay excluded.
    db_session.add(Email(gmail_message_id="d4", sender="n@x.com", subject="new",
                         snippet="fresh", summary="fresh summary", status="classified",
                         classification_id=digest_setup["cat"], confidence=0.95,
                         received_at=datetime.now(UTC)))
    db_session.commit()
    run3 = auth_client.post(f"/api/v1/digests/{d['id']}/run-now").json()
    assert run3["status"] == "success"
    assert len(run3["email_ids"]) == 1


@respx.mock
def test_failed_send_keeps_emails_eligible(auth_client, db_session, digest_setup,
                                           monkeypatch):
    import app.services.telegram as tg_mod
    monkeypatch.setattr(tg_mod, "RETRIES", 1)
    respx.post(TG_SEND).respond(500, json={"ok": False})
    d = digest_setup["digest"]

    run = auth_client.post(f"/api/v1/digests/{d['id']}/run-now").json()
    assert run["status"] == "error"

    # Retry with Telegram healthy: same emails still eligible.
    respx.post(TG_SEND).mock(return_value=tg_ok())
    run2 = auth_client.post(f"/api/v1/digests/{d['id']}/run-now").json()
    assert run2["status"] == "success"
    assert sorted(run2["email_ids"]) == sorted(run["email_ids"])


@respx.mock
def test_empty_digest_skips_silently_but_logs_run(auth_client, digest_setup):
    d = auth_client.post("/api/v1/digests", json={
        "name": "empty one", "category_ids": [], "min_confidence": 0.99}).json()
    tg = respx.post(TG_SEND)

    run = auth_client.post(f"/api/v1/digests/{d['id']}/run-now").json()
    assert run["status"] == "empty"
    assert tg.call_count == 0
    runs = auth_client.get(f"/api/v1/digests/{d['id']}/runs").json()
    assert len(runs) == 1


@respx.mock
def test_max_emails_ignored_in_assemble_mode(auth_client, db_session, digest_setup):
    """In assemble mode max_emails is irrelevant — every eligible email is included."""
    d = digest_setup["digest"]
    auth_client.put(f"/api/v1/digests/{d['id']}", json={
        "name": d["name"], "category_ids": d["category_ids"],
        "cron_times": d["cron_times"], "timezone": d["timezone"],
        "min_confidence": 0.8, "max_emails": 1})
    run = auth_client.post(f"/api/v1/digests/{d['id']}/run-now",
                           json={"preview": True}).json()
    # Both emails qualify (confidence ≥ 0.8); assemble mode ignores max_emails=1
    assert len(run["email_ids"]) == 2


@respx.mock
def test_max_emails_cap_applies_in_synthesize_mode(auth_client, db_session, digest_setup):
    """max_emails cap is enforced in synthesize mode to bound the LLM prompt."""
    mock_llm_text("Synth.")
    respx.post(TG_SEND).mock(return_value=tg_ok())
    d = digest_setup["digest"]
    auth_client.put(f"/api/v1/digests/{d['id']}", json={
        "name": d["name"], "category_ids": d["category_ids"],
        "cron_times": d["cron_times"], "timezone": d["timezone"],
        "min_confidence": 0.8, "max_emails": 1, "mode": "synthesize"})
    run = auth_client.post(f"/api/v1/digests/{d['id']}/run-now").json()
    assert run["status"] == "success"
    assert run["email_ids"] == [digest_setup["e2"]]


@respx.mock
def test_preview_blocked_while_running(auth_client, db_session, digest_setup):
    """A fresh `running` row (preview or real) blocks a new run of either kind."""
    chat = respx.post(CHAT_URL)
    d = digest_setup["digest"]
    existing = DigestRun(digest_id=d["id"], status=DigestRunStatus.running.value,
                         started_at=datetime.now(UTC))
    db_session.add(existing)
    db_session.commit()

    run = auth_client.post(f"/api/v1/digests/{d['id']}/run-now",
                           json={"preview": True}).json()
    assert run["id"] == existing.id            # returned the in-flight run
    assert run["status"] == "running"
    assert chat.call_count == 0                 # guard short-circuited


def test_list_digests_includes_last_run(auth_client, db_session, digest_setup):
    d = digest_setup["digest"]
    db_session.add(DigestRun(digest_id=d["id"], status=DigestRunStatus.success.value,
                             started_at=datetime.now(UTC)))
    db_session.commit()
    listed = auth_client.get("/api/v1/digests").json()
    row = next(x for x in listed if x["id"] == d["id"])
    assert "depth" not in row
    assert row["mode"] == "assemble"
    assert row["email_threshold"] is None
    assert row["last_run"]["status"] == "success"


def test_digest_mode_and_threshold_fields(auth_client, db_session, digest_setup):
    """mode and email_threshold are persisted and returned by the API."""
    d = digest_setup["digest"]
    # Default: assemble, no threshold
    listed = auth_client.get("/api/v1/digests").json()
    row = next(x for x in listed if x["id"] == d["id"])
    assert row["mode"] == "assemble"
    assert row["email_threshold"] is None

    # Update to synthesize with threshold
    auth_client.put(f"/api/v1/digests/{d['id']}", json={
        "name": d["name"], "category_ids": d["category_ids"],
        "cron_times": d["cron_times"], "timezone": d["timezone"],
        "min_confidence": 0.8, "mode": "synthesize", "email_threshold": 10})
    updated = auth_client.get("/api/v1/digests").json()
    row = next(x for x in updated if x["id"] == d["id"])
    assert row["mode"] == "synthesize"
    assert row["email_threshold"] == 10


def test_llm_queue_reports_running_work(auth_client, db_session, digest_setup):
    d = digest_setup["digest"]
    db_session.add(DigestRun(digest_id=d["id"], status=DigestRunStatus.running.value,
                             started_at=datetime.now(UTC)))
    db_session.add(Email(gmail_message_id="proc1", sender="p@x.com", subject="in flight",
                         status=EmailStatus.processing.value,
                         processing_started_at=datetime.now(UTC)))
    db_session.commit()
    q = auth_client.get("/api/v1/llm/queue").json()
    assert any(x["name"] == "Market news" for x in q["digests"])
    assert any(x["subject"] == "in flight" for x in q["processing"])


@respx.mock
def test_synthesize_empty_falls_back_to_summaries(auth_client, db_session, digest_setup):
    """Synthesis blank on both attempts → body falls back to the saved summaries;
    run still succeeds and the message is non-blank."""
    d = digest_setup["digest"]
    _set_digest_mode(db_session, d["id"], "synthesize")
    chat = mock_llm_text("   ")                     # blank on every attempt
    tg = respx.post(TG_SEND).mock(return_value=tg_ok())

    run = auth_client.post(f"/api/v1/digests/{d['id']}/run-now").json()
    assert run["status"] == "success"
    assert "S&P summary" in run["summary_text"]     # fell back to saved summaries
    assert "Bonds summary" in run["summary_text"]
    sent = json.loads(tg.calls[0].request.content)
    assert "Bonds summary" in sent["text"]           # no special chars to escape
    assert chat.call_count == 2                      # synthesis attempted twice


@respx.mock
def test_synthesize_empty_retry_recovers(auth_client, db_session, digest_setup):
    """First synthesis attempt blank, second returns text → the retry text wins."""
    d = digest_setup["digest"]
    _set_digest_mode(db_session, d["id"], "synthesize")
    state = {"n": 0}

    def handler(request):
        state["n"] += 1
        return llm_response("" if state["n"] == 1 else "Recovered body.")

    respx.post(CHAT_URL).mock(side_effect=handler)
    respx.post(TG_SEND).mock(return_value=tg_ok())

    run = auth_client.post(f"/api/v1/digests/{d['id']}/run-now").json()
    assert run["status"] == "success"
    assert run["summary_text"] == "Recovered body."
    assert state["n"] == 2                           # retried exactly once


@respx.mock
def test_no_content_body_errors_without_sending(auth_client, db_session, digest_setup):
    """An email with neither summary nor snippet yields no body → run errors,
    nothing sent (assemble mode)."""
    cat = Category(name="Empty", criteria_md="m")
    db_session.add(cat)
    db_session.flush()
    db_session.add(Email(gmail_message_id="blank1", sender="x@x.com", subject="blank",
                         snippet=None, summary=None, status="classified",
                         classification_id=cat.id, confidence=0.95,
                         received_at=datetime.now(UTC)))
    db_session.commit()
    d = auth_client.post("/api/v1/digests", json={
        "name": "blank digest", "category_ids": [cat.id], "min_confidence": 0.8}).json()
    tg = respx.post(TG_SEND).mock(return_value=tg_ok())

    run = auth_client.post(f"/api/v1/digests/{d['id']}/run-now").json()
    assert run["status"] == "error"
    assert run["error"]
    assert tg.call_count == 0                        # never shipped a blank digest


async def test_fetch_context_length_parses_props():
    from app.services import llm

    settings = {"llm_base_url": "http://host.docker.internal:8081/v1"}
    with respx.mock:
        respx.get("http://host.docker.internal:8081/props").mock(
            return_value=Response(200, json={"n_ctx": 8192}))
        assert await llm.fetch_context_length(settings) == 8192


async def test_fetch_context_length_none_on_failure():
    from app.services import llm

    settings = {"llm_base_url": "http://host.docker.internal:8081/v1"}
    with respx.mock:
        respx.get("http://host.docker.internal:8081/props").mock(
            return_value=Response(404))
        assert await llm.fetch_context_length(settings) is None


def test_render_assemble_timezone():
    """Per-email time in assemble blocks is expressed in the digest's timezone."""
    from zoneinfo import ZoneInfo

    from app.services.digests import _render_assemble_messages

    digest = Digest(name="d", timezone="America/Los_Angeles",
                    include_metadata=True, include_links=False)
    email = Email(gmail_message_id="z1", sender="a@x.com", subject="s",
                  summary="text", received_at=datetime(2026, 6, 12, 18, 30, tzinfo=UTC))
    tz = ZoneInfo("America/Los_Angeles")
    msgs = _render_assemble_messages(digest, [email], dry_run_prefix=False, tz=tz)
    assert len(msgs) == 1
    assert "11:30" in msgs[0]    # 18:30 UTC == 11:30 PDT


def test_render_assemble_invalid_timezone_falls_back_to_utc():
    """An unrecognised timezone in run_digest falls through to UTC."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    from app.services.digests import _render_assemble_messages

    digest = Digest(name="d", timezone="Not/AZone",
                    include_metadata=True, include_links=False)
    email = Email(gmail_message_id="z2", sender="a@x.com", subject="s",
                  summary="text", received_at=datetime(2026, 6, 12, 18, 30, tzinfo=UTC))
    try:
        tz = ZoneInfo("Not/AZone")
    except (KeyError, ZoneInfoNotFoundError):
        from datetime import UTC as _UTC
        tz = _UTC
    msgs = _render_assemble_messages(digest, [email], dry_run_prefix=False, tz=tz)
    assert "18:30" in msgs[0]    # fell back to UTC


def test_render_assemble_per_email_blockquotes():
    """Each email in assemble mode gets its own blockquote with subject link."""
    from zoneinfo import ZoneInfo

    from app.services.digests import _render_assemble_messages

    tz = ZoneInfo("UTC")
    digest = Digest(name="News", timezone="UTC",
                    include_metadata=True, include_links=True)
    email = Email(gmail_message_id="m9", sender="Alice Smith <alice@x.com>",
                  subject="Hello", summary="Stocks fell 2%.",
                  received_at=datetime(2026, 6, 12, 9, 5, tzinfo=UTC))
    msgs = _render_assemble_messages(digest, [email], dry_run_prefix=False, tz=tz)
    assert len(msgs) == 1
    msg = msgs[0]
    assert "📬 <b>News</b>" in msg
    assert "<b>1</b> email" in msg
    assert "📧" in msg
    assert '<a href="https://mail.google.com/mail/u/0/#all/m9">Hello</a>' in msg
    assert "Alice Smith" in msg          # display name, not email address
    assert "alice@x.com" not in msg     # no raw email address
    assert "09:05" in msg
    assert "Stocks fell 2%." in msg
    assert "<blockquote>" in msg


def test_render_assemble_no_links_plain_subject():
    """include_links=False renders subject as bold text instead of a hyperlink."""
    from zoneinfo import ZoneInfo

    from app.services.digests import _render_assemble_messages

    tz = ZoneInfo("UTC")
    digest = Digest(name="News", timezone="UTC",
                    include_metadata=True, include_links=False)
    email = Email(gmail_message_id="m9", sender="a@x.com", subject="Hello",
                  summary="text", received_at=datetime(2026, 6, 12, 9, 5, tzinfo=UTC))
    msgs = _render_assemble_messages(digest, [email], dry_run_prefix=False, tz=tz)
    msg = msgs[0]
    assert "<a href" not in msg
    assert "<b>Hello</b>" in msg


def test_render_assemble_no_metadata_omits_sender_time():
    """include_metadata=False omits the sender · time line."""
    from zoneinfo import ZoneInfo

    from app.services.digests import _render_assemble_messages

    tz = ZoneInfo("UTC")
    digest = Digest(name="News", timezone="UTC",
                    include_metadata=False, include_links=True)
    email = Email(gmail_message_id="m9", sender="a@x.com", subject="Hello",
                  summary="body text",
                  received_at=datetime(2026, 6, 12, 9, 5, tzinfo=UTC))
    msgs = _render_assemble_messages(digest, [email], dry_run_prefix=False, tz=tz)
    msg = msgs[0]
    assert "09:05" not in msg
    assert "a@x.com" not in msg
    assert "body text" in msg
    assert "<blockquote>" in msg


def test_render_assemble_multi_message_split_on_block_boundary():
    """When emails overflow 4080 chars, they spill into a new message; no block is split."""
    from zoneinfo import ZoneInfo

    from app.services.digests import _render_assemble_messages

    tz = ZoneInfo("UTC")
    digest = Digest(name="D", timezone="UTC",
                    include_metadata=False, include_links=False)
    # Each email summary is ~1500 chars → two emails will exceed the 4080-char budget
    big_summary = "x" * 1500
    emails = [
        Email(gmail_message_id=f"m{i}", sender="a@x.com", subject=f"S{i}",
              summary=big_summary, received_at=datetime(2026, 6, 12, tzinfo=UTC))
        for i in range(3)
    ]
    msgs = _render_assemble_messages(digest, emails, dry_run_prefix=False, tz=tz)
    assert len(msgs) > 1
    assert all(len(m) <= 4096 for m in msgs)
    # Each message must have complete blockquotes (not split mid-block)
    for m in msgs:
        assert m.count("<blockquote") == m.count("</blockquote>")
    # Numbered prefix on multi-part messages
    assert msgs[0].startswith("[1/")


def test_render_message_synthesize_tldr_above_blockquote():
    from app.services.digests import _render_message

    digest = Digest(name="News", timezone="UTC",
                    include_metadata=False, include_links=False)
    summary = "TL;DR — two themes today.\nMarkets: stocks fell.\nOps: deploy ok."
    email = Email(gmail_message_id="m9", sender="a@x.com", subject="s",
                  received_at=datetime(2026, 6, 12, 9, 5, tzinfo=UTC))
    msg = _render_message(digest, [email], summary, dry_run_prefix=False)
    assert "<b>TL;DR — two themes today.</b>" in msg
    after_bq = msg.split("<blockquote", 1)[1]
    assert "TL;DR — two themes today." not in after_bq
    assert "Markets: stocks fell." in after_bq
    assert "Ops: deploy ok." in after_bq


def test_render_message_expandable_when_long():
    from app.services.digests import _render_message

    digest = Digest(name="News", timezone="UTC",
                    include_metadata=False, include_links=False)
    summary = "TL;DR.\n" + "\n".join(f"line {i}" for i in range(6))
    email = Email(gmail_message_id="m9", sender="a@x.com", subject="s",
                  received_at=datetime(2026, 6, 12, 9, 5, tzinfo=UTC))
    msg = _render_message(digest, [email], summary, dry_run_prefix=False)
    assert "<blockquote expandable>" in msg


def test_render_message_short_blockquote_not_expandable():
    from app.services.digests import _render_message

    digest = Digest(name="News", timezone="UTC",
                    include_metadata=False, include_links=False)
    summary = "TL;DR.\nshort rest."
    email = Email(gmail_message_id="m9", sender="a@x.com", subject="s",
                  received_at=datetime(2026, 6, 12, 9, 5, tzinfo=UTC))
    msg = _render_message(digest, [email], summary, dry_run_prefix=False)
    assert "<blockquote>" in msg
    assert "expandable" not in msg


def test_strip_summary_html():
    from app.services.classifier import strip_summary_html

    assert strip_summary_html("Hello &amp; world") == "Hello & world"
    assert strip_summary_html("<b>bold</b> text&nbsp;here") == "bold text here"
    assert strip_summary_html("no html") == "no html"
    assert strip_summary_html("a  b\n\nc") == "a b c"


def test_display_name_extraction():
    from app.services.digests import _display_name

    assert _display_name("Alice Smith <alice@x.com>") == "Alice Smith"
    assert _display_name('"Bob Jones" <bob@y.com>') == "Bob Jones"
    assert _display_name("plain@email.com") == "plain@email.com"
    assert _display_name(None) == "?"
    assert _display_name("") == "?"


def test_normalize_summary_collapses_whitespace():
    from app.services.digests import _normalize_summary

    raw = "line one   \n\n\n\nline two\n\n"
    assert _normalize_summary(raw) == "line one\n\nline two"
