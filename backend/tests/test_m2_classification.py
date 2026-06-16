"""M2 acceptance: categories CRUD, classification pipeline with mocked LLM,
invalid-output retry semantics, LLM-down queueing."""

import json

import pytest
import respx
from httpx import Response

from tests.test_m1_gmail import (
    CLIENT_SECRET_JSON,
    b64url,
    gmail_message,
    make_token,
)

LLM_BASE = "http://host.docker.internal:8081/v1"
CHAT_URL = f"{LLM_BASE}/chat/completions"


def llm_response(payload: dict | str) -> Response:
    content = payload if isinstance(payload, str) else json.dumps(payload)
    return Response(200, json={
        "id": "x", "object": "chat.completion", "created": 0, "model": "local",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": content}}],
    })


def classify_then_summarize(responses: list[Response], summary: str = "auto summary."):
    """respx side_effect: serve `responses` (in order) to the schema-constrained
    classification calls, and a fixed plain-text `summary` to the summarization
    call that now fires after every successful classification."""
    it = iter(responses)

    def handler(request):
        if '"json_schema"' in request.content.decode():
            return next(it)
        return llm_response(summary)

    return handler


def schema_call_count(route) -> int:
    """How many of a route's calls were classification (schema) calls."""
    return sum('"json_schema"' in c.request.content.decode() for c in route.calls)


@pytest.fixture()
def connected(client, db_session):
    from app.services import gmail, settings_service

    settings_service.set_setting(db_session, "gmail_client_secret_json", CLIENT_SECRET_JSON)
    gmail.save_token(db_session, make_token(), email="me@gmail.test")
    db_session.commit()


@pytest.fixture()
def seeded(auth_client, db_session, connected):
    """Two categories + one pending email with a fetchable body."""
    for name, criteria in [("MarketNews", "Market commentary and macro analysis."),
                           ("Receipts", "Order confirmations and invoices.")]:
        resp = auth_client.post("/api/v1/categories",
                                json={"name": name, "criteria_md": criteria})
        assert resp.status_code == 201

    from app.models import Email
    db_session.add(Email(gmail_message_id="m1", sender="Brew <crew@brew.com>",
                         sender_domain="brew.com", subject="Stocks slide",
                         snippet="Futures fell", status="pending"))
    db_session.commit()

    full = gmail_message("m1")
    full["payload"]["parts"] = [
        {"mimeType": "text/plain", "body": {"data": b64url("Futures fell sharply today.")}}]
    return full


def mock_gmail_full(full_msg):
    from app.services.gmail import GMAIL_API
    respx.get(f"{GMAIL_API}/messages/{full_msg['id']}").respond(200, json=full_msg)


# ── Categories CRUD ──────────────────────────────────────────────────────────

def test_category_crud_and_history(auth_client):
    resp = auth_client.post("/api/v1/categories", json={
        "name": "MarketNews", "criteria_md": "v1 criteria"})
    assert resp.status_code == 201
    cat = resp.json()
    assert cat["criteria_version"] == 1

    # duplicate name rejected
    assert auth_client.post("/api/v1/categories",
                            json={"name": "MarketNews"}).status_code == 409

    # criteria change bumps version + history
    resp = auth_client.put(f"/api/v1/categories/{cat['id']}", json={
        "name": "MarketNews", "criteria_md": "v2 criteria"})
    assert resp.json()["criteria_version"] == 2
    history = auth_client.get(f"/api/v1/categories/{cat['id']}/criteria-history").json()
    assert [h["version"] for h in history] == [2, 1]
    assert history[0]["source"] == "user"

    # non-criteria change does NOT bump version
    resp = auth_client.put(f"/api/v1/categories/{cat['id']}", json={
        "name": "MarketNews", "criteria_md": "v2 criteria", "enabled": False})
    assert resp.json()["criteria_version"] == 2

    assert auth_client.delete(f"/api/v1/categories/{cat['id']}").status_code == 200
    assert auth_client.get("/api/v1/categories").json() == []


# ── Classification pipeline ──────────────────────────────────────────────────

@respx.mock
def test_classify_pending_happy_path(auth_client, db_session, seeded):
    mock_gmail_full(seeded)
    chat = respx.post(CHAT_URL).mock(return_value=llm_response({
        "category": "MarketNews", "confidence": 0.92, "rationale": "Market commentary."}))

    resp = auth_client.post("/api/v1/classify/run-now")
    assert resp.status_code == 200
    assert resp.json()["classified"] == 1

    from app.models import Category, Email
    email = db_session.query(Email).one()
    market = db_session.query(Category).filter_by(name="MarketNews").one()
    assert email.status == "classified"
    assert email.classification_id == market.id
    assert email.confidence == 0.92
    assert email.rationale == "Market commentary."
    assert email.body_text is None          # store_bodies defaults to false
    assert email.body_text_hash is not None

    # request contained criteria + truncated body + json_schema enforcement
    request_body = json.loads(chat.calls[0].request.content)
    assert request_body["temperature"] == 0
    assert request_body["response_format"]["type"] == "json_schema"
    schema = request_body["response_format"]["json_schema"]["schema"]
    assert set(schema["properties"]["category"]["enum"]) == {
        "MarketNews", "Receipts", "none"}
    user_msg = request_body["messages"][1]["content"]
    assert "Market commentary and macro analysis." in user_msg
    assert "Futures fell sharply today." in user_msg


@respx.mock
def test_classification_saves_summary_at_configured_depth(auth_client, db_session, seeded):
    """A successful classification also stores a per-email summary, generated with
    the prompt for the configured summarization depth."""
    auth_client.put("/api/v1/settings", json={"summarization_depth": "extended"})
    mock_gmail_full(seeded)
    chat = respx.post(CHAT_URL).mock(side_effect=classify_then_summarize(
        [llm_response({"category": "MarketNews", "confidence": 0.9, "rationale": "r"})],
        summary="A thorough recap of the futures move."))

    auth_client.post("/api/v1/classify/run-now")

    from app.models import Email
    email = db_session.query(Email).one()
    assert email.status == "classified"
    assert email.summary == "A thorough recap of the futures move."
    # The summary call used the extended-depth system prompt.
    summary_call = next(c for c in chat.calls
                        if '"json_schema"' not in c.request.content.decode())
    system_msg = json.loads(summary_call.request.content)["messages"][0]["content"]
    assert "thoroughly" in system_msg


@respx.mock
def test_summary_failure_keeps_classification(auth_client, db_session, seeded):
    """If the summarization call fails, the email stays classified (summary NULL)."""
    mock_gmail_full(seeded)

    def handler(request):
        if '"json_schema"' in request.content.decode():
            return llm_response({"category": "MarketNews", "confidence": 0.9,
                                 "rationale": "r"})
        return Response(500, json={"error": "boom"})  # summary call fails

    respx.post(CHAT_URL).mock(side_effect=handler)
    auth_client.post("/api/v1/classify/run-now")

    from app.models import Email
    email = db_session.query(Email).one()
    assert email.status == "classified"
    assert email.summary is None


@respx.mock
def test_hard_rule_classification_has_no_summary(auth_client, db_session, seeded):
    """A sender hard-rule bypasses the LLM entirely — no classification and no
    summary call — so the email has no saved summary."""
    auth_client.post("/api/v1/rules", json={
        "name": "brew", "match_sender_pattern": "*@brew.com",
        "actions": [{"type": "mark_read"}]})
    chat = respx.post(CHAT_URL)
    auth_client.post("/api/v1/classify/run-now")

    from app.models import Email
    email = db_session.query(Email).one()
    assert email.status in ("classified", "actioned")
    assert email.summary is None
    assert chat.call_count == 0


@respx.mock
def test_invalid_output_one_retry_then_success(auth_client, db_session, seeded):
    mock_gmail_full(seeded)
    chat = respx.post(CHAT_URL).mock(side_effect=classify_then_summarize([
        llm_response("I think this is market news!"),  # invalid (no JSON)
        llm_response({"category": "MarketNews", "confidence": 0.8, "rationale": "ok"}),
    ]))
    resp = auth_client.post("/api/v1/classify/run-now")
    assert resp.json()["classified"] == 1
    assert schema_call_count(chat) == 2  # one retry (summary call excluded)


@respx.mock
def test_invalid_output_twice_marks_error(auth_client, db_session, seeded):
    mock_gmail_full(seeded)
    chat = respx.post(CHAT_URL)
    chat.side_effect = [
        llm_response("not json"),
        llm_response({"category": "Bogus", "confidence": 2}),  # enum+schema violation
    ]
    resp = auth_client.post("/api/v1/classify/run-now")
    assert resp.json()["errors"] == 1
    assert chat.call_count == 2  # exactly one retry

    from app.models import Email
    email = db_session.query(Email).one()
    assert email.status == "error"
    assert "invalid" in email.error.lower()


@respx.mock
def test_none_category(auth_client, db_session, seeded):
    mock_gmail_full(seeded)
    respx.post(CHAT_URL).mock(return_value=llm_response({
        "category": "none", "confidence": 0.7, "rationale": "No criteria apply."}))
    auth_client.post("/api/v1/classify/run-now")

    from app.models import Email
    email = db_session.query(Email).one()
    assert email.status == "classified"
    assert email.classification_id is None
    assert email.confidence == 0.7


@respx.mock
def test_ignore_list_skips_without_llm_call(auth_client, db_session, seeded):
    auth_client.put("/api/v1/settings", json={"ignore_senders": ["*@brew.com"]})
    chat = respx.post(CHAT_URL)
    resp = auth_client.post("/api/v1/classify/run-now")
    assert resp.json() == {"classified": 0, "skipped": 1, "errors": 0,
                           "actioned": 0, "pending_left": 0}
    assert chat.call_count == 0

    from app.models import Email
    assert db_session.query(Email).one().status == "skipped"


@respx.mock
def test_llm_down_leaves_pending(auth_client, db_session, seeded):
    import httpx

    mock_gmail_full(seeded)
    respx.post(CHAT_URL).mock(side_effect=httpx.ConnectError("refused"))
    resp = auth_client.post("/api/v1/classify/run-now")
    assert resp.status_code == 200
    assert resp.json()["pending_left"] == 1

    from app.models import Email
    assert db_session.query(Email).one().status == "pending"
    status = auth_client.get("/api/v1/status").json()
    assert status["llm"]["status"] == "unreachable"


@respx.mock
def test_body_truncation(auth_client, db_session, seeded, connected):
    auth_client.put("/api/v1/settings", json={"classify_body_max_chars": 50})
    seeded["payload"]["parts"][0]["body"]["data"] = b64url("X" * 500)
    mock_gmail_full(seeded)
    chat = respx.post(CHAT_URL).mock(return_value=llm_response({
        "category": "none", "confidence": 0.5, "rationale": "r"}))
    auth_client.post("/api/v1/classify/run-now")
    user_msg = json.loads(chat.calls[0].request.content)["messages"][1]["content"]
    assert "X" * 50 in user_msg
    assert "X" * 51 not in user_msg


@respx.mock
def test_store_bodies_setting(auth_client, db_session, seeded):
    auth_client.put("/api/v1/settings", json={"store_bodies": True})
    mock_gmail_full(seeded)
    respx.post(CHAT_URL).mock(return_value=llm_response({
        "category": "none", "confidence": 0.5, "rationale": "r"}))
    auth_client.post("/api/v1/classify/run-now")

    from app.models import Email
    assert db_session.query(Email).one().body_text == "Futures fell sharply today."


@respx.mock
def test_llm_health_endpoint(auth_client):
    respx.get(f"{LLM_BASE}/models").respond(200, json={
        "object": "list", "data": [{"id": "qwen", "object": "model",
                                    "created": 0, "owned_by": "me"}]})
    resp = auth_client.post("/api/v1/llm/test")
    assert resp.json()["ok"] is True
    assert resp.json()["models"] == ["qwen"]


# ── per-email reclassify ─────────────────────────────────────────────────────

@respx.mock
def test_reclassify_single_email(auth_client, db_session, seeded):
    mock_gmail_full(seeded)
    respx.post(CHAT_URL).mock(side_effect=classify_then_summarize([
        llm_response({"category": "MarketNews", "confidence": 0.9, "rationale": "v1"}),
        llm_response({"category": "Receipts", "confidence": 0.7, "rationale": "v2"}),
    ]))
    # Rule plans a dry-run action on first classification.
    auth_client.post("/api/v1/rules", json={
        "name": "label market", "match_category_id": 1,
        "actions": [{"type": "mark_read"}]})
    auth_client.post("/api/v1/classify/run-now")

    from app.models import Email, EmailAction
    email = db_session.query(Email).one()
    assert email.rationale == "v1"
    assert db_session.query(EmailAction).count() == 1  # stale dry-run plan

    # Reclassify returns pending immediately (queue handles re-classification).
    resp = auth_client.post(f"/api/v1/emails/{email.id}/reclassify")
    assert resp.status_code == 200
    detail = resp.json()
    assert detail["status"] == "pending"
    assert detail["classification"] is None
    assert detail["summary"] is None              # stale summary cleared
    assert db_session.query(EmailAction).count() == 0  # all actions cleared

    # run-now drives the classification synchronously in tests.
    auth_client.post("/api/v1/classify/run-now")
    db_session.expire_all()
    email = db_session.query(Email).one()
    assert email.rationale == "v2"
    assert email.confidence == 0.7
    assert email.status == "classified"


@respx.mock
def test_reclassify_clears_all_actions(auth_client, db_session, seeded):
    """Reclassify wipes ALL previous actions (executed and planned alike)."""
    from datetime import UTC, datetime

    from app.models import Email, EmailAction
    email = db_session.query(Email).one()
    db_session.add(EmailAction(email_id=email.id, action_type="archive",
                               executed=True, dry_run=False,
                               executed_at=datetime.now(UTC)))
    db_session.commit()

    resp = auth_client.post(f"/api/v1/emails/{email.id}/reclassify")
    assert resp.status_code == 200
    db_session.expire_all()
    assert db_session.query(EmailAction).count() == 0  # all cleared


@respx.mock
def test_reclassify_llm_down_leaves_pending(auth_client, db_session, seeded):
    """Reclassify returns 200 pending; run-now with LLM down leaves it pending."""
    import httpx

    from app.models import Email
    email = db_session.query(Email).one()
    resp = auth_client.post(f"/api/v1/emails/{email.id}/reclassify")
    assert resp.status_code == 200
    assert resp.json()["status"] == "pending"

    # LLM is unreachable — run-now should leave the email pending (not error).
    mock_gmail_full(seeded)
    respx.post(CHAT_URL).mock(side_effect=httpx.ConnectError("refused"))
    auth_client.post("/api/v1/classify/run-now")
    db_session.expire_all()
    assert db_session.query(Email).one().status == "pending"


@respx.mock
def test_llm_timeout_not_unavailable(auth_client, db_session, seeded):
    """APITimeoutError marks the email as error but does NOT set LLM unreachable."""
    import httpx

    from app.models import Email
    from app.state import app_state

    app_state.llm_status = "unknown"  # isolate from other tests that set unreachable

    mock_gmail_full(seeded)
    respx.post(CHAT_URL).mock(side_effect=httpx.ReadTimeout("timed out"))
    resp = auth_client.post("/api/v1/classify/run-now")
    assert resp.status_code == 200

    db_session.expire_all()
    email = db_session.query(Email).one()
    assert email.status == "error"
    assert "timed out" in (email.error or "").lower()

    # LLM should NOT be marked unreachable after a timeout.
    assert app_state.llm_status != "unreachable"


def test_reclassify_unknown_email_404(auth_client, connected):
    assert auth_client.post("/api/v1/emails/9999/reclassify").status_code == 404


@respx.mock
def test_classify_marks_error_when_message_deleted(auth_client, db_session, seeded):
    """A 404 fetching the body (message deleted) marks the email error, not a
    500 that aborts the batch."""
    from app.services.gmail import GMAIL_API
    respx.get(f"{GMAIL_API}/messages/m1").respond(404, json={"error": "nf"})
    chat = respx.post(CHAT_URL)
    resp = auth_client.post("/api/v1/classify/run-now")
    assert resp.status_code == 200
    assert resp.json()["errors"] == 1
    assert chat.call_count == 0  # never reached the LLM

    from app.models import Email
    email = db_session.query(Email).one()
    assert email.status == "error"
    assert "no longer available" in email.error
