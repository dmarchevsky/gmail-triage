"""Decider (System One) classification backend: request shape, category
mapping, error semantics, non-fatal summaries, settings validation, and the
side-by-side preview endpoint."""

import json

import httpx
import pytest
import respx
from httpx import Response

from tests import test_m2_classification as m2
from tests.test_m1_gmail import b64url
from tests.test_m2_classification import CHAT_URL, LLM_BASE, llm_response, mock_gmail_full

# Reuse the M2 fixtures (two categories + one pending email with a body).
connected = m2.connected
seeded = m2.seeded

SYSTEMONE_URL = f"{LLM_BASE}/systemone"


def decider_response(choice: str, probs: dict[str, float],
                     confidence: float | None = 0.95) -> Response:
    answer: dict = {"type": "choice", "choice": choice, "probabilities": probs}
    if confidence is not None:
        answer["confidence"] = confidence
    return Response(200, json={"model": "d3-nano", "answers": {"category": answer},
                               "usage": {"input_tokens": 100, "output_tokens": 2}})


@pytest.fixture()
def decider_mode(auth_client):
    resp = auth_client.put("/api/v1/settings", json={"classifier_backend": "decider"})
    assert resp.status_code == 200


@pytest.fixture()
def snippet_summaries(auth_client, decider_mode):
    auth_client.put("/api/v1/settings", json={"decider_summary_source": "snippet"})


def _email(db_session):
    from app.models import Email
    db_session.expire_all()
    return db_session.query(Email).one()


@respx.mock
def test_decider_classifies_and_summarizes(auth_client, db_session, seeded, decider_mode):
    mock_gmail_full(seeded)
    route = respx.post(SYSTEMONE_URL).mock(return_value=decider_response(
        "MarketNews", {"MarketNews": 0.97, "Receipts": 0.02, "none": 0.01}, 0.96))
    chat = respx.post(CHAT_URL).mock(return_value=llm_response("Futures fell sharply."))

    resp = auth_client.post("/api/v1/classify/run-now")
    assert resp.json()["classified"] == 1

    from app.models import Category
    email = _email(db_session)
    market = db_session.query(Category).filter_by(name="MarketNews").one()
    assert email.status == "classified"
    assert email.classification_id == market.id
    assert email.confidence == 0.96
    assert email.llm_model == "d3-nano"
    assert email.rationale == "decider d3-nano · p=0.96 · runner-up: Receipts 0.02"
    assert email.summary == "Futures fell sharply."

    body = json.loads(route.calls[0].request.content)
    assert body["model"] == "d3-nano"
    question = body["questions"]["category"]
    assert question["type"] == "choice"
    assert question["criteria"] == {
        "MarketNews": "Market commentary and macro analysis.",
        "Receipts": "Order confirmations and invoices.",
        "none": "None of the categories' criteria apply."}
    assert "Futures fell sharply today." in body["state"]
    assert "Subject: Stocks slide" in body["state"]

    # summary is a plain-text call (no json_schema), separate from classification
    chat_body = json.loads(chat.calls[0].request.content)
    assert "response_format" not in chat_body
    assert "Futures fell sharply today." in chat_body["messages"][1]["content"]


@respx.mock
def test_decider_none_choice_leaves_uncategorized(auth_client, db_session, seeded,
                                                  snippet_summaries):
    mock_gmail_full(seeded)
    respx.post(SYSTEMONE_URL).mock(return_value=decider_response(
        "none", {"MarketNews": 0.1, "Receipts": 0.1, "none": 0.8}, confidence=None))
    auth_client.post("/api/v1/classify/run-now")

    email = _email(db_session)
    assert email.status == "classified"
    assert email.classification_id is None
    assert email.confidence == 0.8  # falls back to the chosen option's probability


@respx.mock
def test_snippet_mode_makes_no_generative_call(auth_client, db_session, seeded,
                                               snippet_summaries):
    mock_gmail_full(seeded)
    respx.post(SYSTEMONE_URL).mock(return_value=decider_response(
        "Receipts", {"MarketNews": 0.1, "Receipts": 0.9, "none": 0.0}))
    chat = respx.post(CHAT_URL).mock(return_value=llm_response("x"))
    auth_client.post("/api/v1/classify/run-now")

    assert chat.call_count == 0
    email = _email(db_session)
    assert email.status == "classified"
    assert email.summary is None


@respx.mock
def test_summary_llm_down_keeps_classification(auth_client, db_session, seeded,
                                               decider_mode):
    mock_gmail_full(seeded)
    respx.post(SYSTEMONE_URL).mock(return_value=decider_response(
        "MarketNews", {"MarketNews": 0.9, "Receipts": 0.05, "none": 0.05}))
    respx.post(CHAT_URL).mock(side_effect=httpx.ConnectError("refused"))
    auth_client.post("/api/v1/classify/run-now")

    email = _email(db_session)
    assert email.status == "classified"
    assert email.classification_id is not None
    assert email.summary is None


@respx.mock
def test_decider_unreachable_leaves_pending(auth_client, db_session, seeded, decider_mode):
    mock_gmail_full(seeded)
    respx.post(SYSTEMONE_URL).mock(side_effect=httpx.ConnectError("refused"))
    resp = auth_client.post("/api/v1/classify/run-now")
    assert resp.json()["pending_left"] == 1
    assert _email(db_session).status == "pending"
    assert auth_client.get("/api/v1/status").json()["llm"]["status"] == "unreachable"


@respx.mock
def test_decider_timeout_marks_error(auth_client, db_session, seeded, decider_mode):
    from app.state import app_state
    app_state.llm_status = "unknown"

    mock_gmail_full(seeded)
    respx.post(SYSTEMONE_URL).mock(side_effect=httpx.ReadTimeout("timed out"))
    auth_client.post("/api/v1/classify/run-now")

    email = _email(db_session)
    assert email.status == "error"
    assert "decider timed out" in (email.error or "").lower()
    assert app_state.llm_status != "unreachable"


@respx.mock
@pytest.mark.parametrize("response", [
    Response(422, json={"detail": "prompt too long"}),
    Response(200, json={"answers": {}}),
    Response(200, json={"answers": {"category": {"choice": "Bogus", "probabilities": {}}}}),
])
def test_decider_bad_answers_mark_error(auth_client, db_session, seeded, decider_mode,
                                        response):
    mock_gmail_full(seeded)
    respx.post(SYSTEMONE_URL).mock(return_value=response)
    auth_client.post("/api/v1/classify/run-now")

    email = _email(db_session)
    assert email.status == "error"
    assert (email.error or "").startswith("Decider output invalid")


@respx.mock
def test_body_trimmed_to_fit_prompt_budget(auth_client, db_session, seeded, decider_mode):
    from app.services import decider

    seeded["payload"]["parts"][0]["body"]["data"] = b64url("x" * 100_000)
    auth_client.put("/api/v1/settings", json={"classify_body_max_chars": 1_000_000,
                                              "decider_summary_source": "snippet"})
    mock_gmail_full(seeded)
    route = respx.post(SYSTEMONE_URL).mock(return_value=decider_response(
        "none", {"none": 1.0}))
    auth_client.post("/api/v1/classify/run-now")

    state = json.loads(route.calls[0].request.content)["state"]
    budget = (decider._PROMPT_TOKEN_CAP - decider._TEMPLATE_RESERVE_TOKENS) \
        * decider._CHARS_PER_TOKEN
    assert 1000 < len(state) < budget


def test_oversized_criteria_raise_invalid_output():
    from app.models import Category, Email
    from app.services import decider, llm

    cats = [Category(id=1, name="Huge", criteria_md="y" * 40_000)]
    with pytest.raises(llm.LLMInvalidOutput, match="shorten the criteria"):
        decider.build_state(Email(subject="s"), "body", decider.build_options(cats), 2000)


def test_resolve_target_defaults_and_trims_systemone():
    from app.services import decider

    assert decider.resolve_decider_target({}) == (LLM_BASE, "d3-nano")
    assert decider.resolve_decider_target({
        "decider_base_url": "http://halogen:8731/v1/systemone/",
        "decider_model": "d3-lite"}) == ("http://halogen:8731/v1", "d3-lite")


@pytest.mark.parametrize("values", [{"classifier_backend": "gpt"},
                                    {"decider_summary_source": "none"}])
def test_enum_settings_rejected(auth_client, values):
    assert auth_client.put("/api/v1/settings", json=values).status_code == 400


@respx.mock
def test_decider_models_filters_decision_task(auth_client):
    respx.get(f"{LLM_BASE}/models").respond(200, json={"data": [
        {"id": "Qwen3.8-Flash-Next"},
        {"id": "d3-nano", "task": "decision"},
        {"id": "d3-lite", "task": "decision"},
        {"id": "qwen3guard", "task": "moderation"}]})
    data = auth_client.post("/api/v1/llm/decider/models", json={}).json()
    assert data["ok"] is True
    assert data["models"] == ["d3-lite", "d3-nano"]


@respx.mock
def test_decider_test_endpoint(auth_client):
    respx.post("http://halogen:8731/v1/systemone").mock(return_value=decider_response(
        "shipping", {"shipping": 0.99, "none": 0.01}))
    data = auth_client.post("/api/v1/llm/decider/test",
                            json={"base_url": "http://halogen:8731/v1"}).json()
    assert data["ok"] is True
    assert data["choice"] == "shipping"
    assert data["base_url"] == "http://halogen:8731/v1"

    respx.post(SYSTEMONE_URL).mock(side_effect=httpx.ConnectError("refused"))
    data = auth_client.post("/api/v1/llm/decider/test").json()
    assert data["ok"] is False


@respx.mock
def test_preview_runs_both_without_changing_email(auth_client, db_session, seeded):
    mock_gmail_full(seeded)
    respx.post(CHAT_URL).mock(return_value=llm_response({
        "category": "MarketNews", "confidence": 0.9, "rationale": "markets",
        "summary": "s"}))
    respx.post(SYSTEMONE_URL).mock(return_value=decider_response(
        "Receipts", {"MarketNews": 0.3, "Receipts": 0.6, "none": 0.1}, 0.6))

    email_id = _email(db_session).id
    data = auth_client.post(f"/api/v1/llm/decider/preview/{email_id}").json()
    assert data["active_backend"] == "llm"
    assert data["llm"]["ok"] and data["llm"]["category"] == "MarketNews"
    assert data["decider"]["ok"] and data["decider"]["category"] == "Receipts"
    assert list(data["decider"]["probabilities"]) == ["Receipts", "MarketNews", "none"]

    email = _email(db_session)
    assert email.status == "pending"
    assert email.classification_id is None


@respx.mock
def test_preview_reports_backend_failure_per_side(auth_client, db_session, seeded):
    mock_gmail_full(seeded)
    respx.post(CHAT_URL).mock(side_effect=httpx.ConnectError("refused"))
    respx.post(SYSTEMONE_URL).mock(return_value=decider_response(
        "Receipts", {"Receipts": 1.0}))
    data = auth_client.post(
        f"/api/v1/llm/decider/preview/{_email(db_session).id}").json()
    assert data["llm"]["ok"] is False
    assert data["decider"]["ok"] is True


def test_preview_unknown_email_404(auth_client):
    assert auth_client.post("/api/v1/llm/decider/preview/999").status_code == 404
