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
               snippet="S&P rose 1%", status="classified",
               classification_id=cat.id, confidence=0.9,
               received_at=now - timedelta(hours=2))
    e2 = Email(gmail_message_id="d2", sender="b@y.com", subject="Bonds <down>",
               snippet="Yields rose", status="classified",
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


def mock_llm_text(text="Summary text."):
    return respx.post(CHAT_URL).mock(return_value=llm_response(text))


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


def _batched_llm():
    """Numbered list for the batched micro-summary call, plain text for the
    synthesis call (distinguished by the synthesis user prompt)."""
    def handler(request):
        body = request.content.decode()
        if "Produce the digest now" in body:
            return llm_response("Summary text.")
        return llm_response("[1] Micro one.\n[2] Micro two.")
    return respx.post(CHAT_URL).mock(side_effect=handler)


@respx.mock
def test_preview_renders_without_sending(auth_client, db_session, digest_setup):
    chat = _batched_llm()
    tg = respx.post(TG_SEND)
    d = digest_setup["digest"]

    run = auth_client.post(f"/api/v1/digests/{d['id']}/run-now",
                           json={"preview": True}).json()
    assert run["status"] == "dry_run"
    assert sorted(run["email_ids"]) == sorted([digest_setup["e2"], digest_setup["e1"]])
    assert run["summary_text"] == "Summary text."
    assert tg.call_count == 0                       # nothing sent in preview
    assert chat.call_count == 2                     # 1 batched micro + 1 synthesis

    # Preview must NOT consume eligibility: preview again, same emails eligible.
    run2 = auth_client.post(f"/api/v1/digests/{d['id']}/run-now",
                            json={"preview": True}).json()
    assert sorted(run2["email_ids"]) == sorted(run["email_ids"])


@respx.mock
def test_live_send_and_watermark_no_email_twice(auth_client, db_session, digest_setup):
    mock_llm_text()
    tg = respx.post(TG_SEND).mock(return_value=tg_ok())
    d = digest_setup["digest"]

    run = auth_client.post(f"/api/v1/digests/{d['id']}/run-now").json()
    assert run["status"] == "success"
    assert len(run["email_ids"]) == 2  # low-confidence excluded
    sent = json.loads(tg.calls[0].request.content)
    assert sent["parse_mode"] == "HTML"
    assert "Bonds &lt;down&gt;" in sent["text"]     # HTML-escaped subject
    assert "mail.google.com" in sent["text"]        # deep links on

    # Second run: nothing new -> empty, no email summarized twice.
    run2 = auth_client.post(f"/api/v1/digests/{d['id']}/run-now").json()
    assert run2["status"] == "empty"
    assert run2["email_ids"] == []

    # New email after watermark becomes eligible; old ones stay excluded.
    db_session.add(Email(gmail_message_id="d4", sender="n@x.com", subject="new",
                         snippet="fresh", status="classified",
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
    mock_llm_text()
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
def test_max_emails_cap_newest_first(auth_client, db_session, digest_setup):
    d = digest_setup["digest"]
    auth_client.put(f"/api/v1/digests/{d['id']}", json={
        "name": d["name"], "category_ids": d["category_ids"],
        "cron_times": d["cron_times"], "timezone": d["timezone"],
        "min_confidence": 0.8, "max_emails": 1})
    mock_llm_text()
    run = auth_client.post(f"/api/v1/digests/{d['id']}/run-now",
                           json={"preview": True}).json()
    assert run["email_ids"] == [digest_setup["e2"]]  # newest of the two


@respx.mock
def test_preview_blocked_while_running(auth_client, db_session, digest_setup):
    """A fresh `running` row (preview or real) blocks a new run of either kind."""
    chat = mock_llm_text()
    d = digest_setup["digest"]
    existing = DigestRun(digest_id=d["id"], status=DigestRunStatus.running.value,
                         started_at=datetime.now(UTC))
    db_session.add(existing)
    db_session.commit()

    run = auth_client.post(f"/api/v1/digests/{d['id']}/run-now",
                           json={"preview": True}).json()
    assert run["id"] == existing.id            # returned the in-flight run
    assert run["status"] == "running"
    assert chat.call_count == 0                 # guard short-circuited; no LLM


def test_list_digests_includes_last_run_and_depth(auth_client, db_session, digest_setup):
    d = digest_setup["digest"]
    db_session.add(DigestRun(digest_id=d["id"], status=DigestRunStatus.success.value,
                             started_at=datetime.now(UTC)))
    db_session.commit()
    listed = auth_client.get("/api/v1/digests").json()
    row = next(x for x in listed if x["id"] == d["id"])
    assert row["depth"] == 2                    # default standard
    assert row["last_run"]["status"] == "success"


def test_depth_roundtrips(auth_client, digest_setup):
    d = digest_setup["digest"]
    auth_client.put(f"/api/v1/digests/{d['id']}", json={
        "name": d["name"], "category_ids": d["category_ids"],
        "cron_times": d["cron_times"], "timezone": d["timezone"],
        "min_confidence": 0.8, "depth": 1})
    row = auth_client.get("/api/v1/digests").json()[0]
    assert row["depth"] == 1
    bad = auth_client.post("/api/v1/digests", json={"name": "x", "depth": 9})
    assert bad.status_code == 422


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
def test_batch_parse_falls_back_when_unnumbered(auth_client, db_session, digest_setup):
    """Two emails → one batch call; an unparseable response falls back to
    one summary call per email, then synthesis (1 + 2 + 1)."""
    chat = respx.post(CHAT_URL).mock(return_value=llm_response("not numbered"))
    d = digest_setup["digest"]
    auth_client.post(f"/api/v1/digests/{d['id']}/run-now", json={"preview": True})
    assert chat.call_count == 4


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


def test_render_message_uses_digest_timezone():
    from app.services.digests import _render_message

    digest = Digest(name="d", timezone="America/Los_Angeles",
                    include_metadata=True, include_links=False)
    email = Email(gmail_message_id="z1", sender="a@x.com", subject="s",
                  received_at=datetime(2026, 6, 12, 18, 30, tzinfo=UTC))
    msg = _render_message(digest, [email], "summary", dry_run_prefix=False)
    assert "• 11:30 " in msg          # 18:30 UTC == 11:30 PDT

    digest.timezone = "Not/AZone"
    msg = _render_message(digest, [email], "summary", dry_run_prefix=False)
    assert "• 18:30 " in msg          # invalid tz falls back to UTC
