"""Sender pins: precedence in classify_one, summaries for pinned mail, the
category API, and pinning from feedback."""

import json

import httpx
import respx

from tests import test_m2_classification as m2
from tests.test_m2_classification import CHAT_URL, llm_response, mock_gmail_full

connected = m2.connected
seeded = m2.seeded

SYSTEMONE_URL = f"{m2.LLM_BASE}/systemone"


def _category(db_session, name):
    from app.models import Category
    return db_session.query(Category).filter_by(name=name).one()


def _pin(auth_client, db_session, name, patterns):
    db_session.expire_all()
    cat = _category(db_session, name)
    resp = auth_client.put(f"/api/v1/categories/{cat.id}", json={
        "name": cat.name, "criteria_md": cat.criteria_md, "sender_patterns": patterns})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _email(db_session):
    from app.models import Email
    db_session.expire_all()
    return db_session.query(Email).one()


@respx.mock
def test_pinned_sender_skips_model_and_summarizes(auth_client, db_session, seeded):
    _pin(auth_client, db_session, "Receipts", ["*@brew.com"])
    mock_gmail_full(seeded)
    chat = respx.post(CHAT_URL).mock(return_value=llm_response("Futures fell."))
    decide = respx.post(SYSTEMONE_URL).mock(return_value=httpx.Response(500))

    assert auth_client.post("/api/v1/classify/run-now").json()["classified"] == 1
    email = _email(db_session)
    assert email.classification_id == _category(db_session, "Receipts").id
    assert email.confidence == 1.0
    assert email.rationale == "pinned sender: *@brew.com"
    assert email.llm_model is None
    assert email.summary == "Futures fell."
    assert decide.call_count == 0
    # one plain-text summary call, no classification schema
    assert chat.call_count == 1
    assert "response_format" not in json.loads(chat.calls[0].request.content)


@respx.mock
def test_pinned_decider_snippet_mode_makes_no_calls(auth_client, db_session, seeded):
    auth_client.put("/api/v1/settings", json={"classifier_backend": "decider",
                                              "decider_summary_source": "snippet"})
    _pin(auth_client, db_session, "MarketNews", ["crew@brew.com"])
    chat = respx.post(CHAT_URL).mock(return_value=llm_response("x"))

    auth_client.post("/api/v1/classify/run-now")
    email = _email(db_session)
    assert email.classification_id == _category(db_session, "MarketNews").id
    assert email.summary is None
    assert chat.call_count == 0  # and no Gmail fetch either (not mocked)


@respx.mock
def test_summary_failure_keeps_pinned_classification(auth_client, db_session, seeded):
    _pin(auth_client, db_session, "Receipts", ["brew.com"])
    mock_gmail_full(seeded)
    respx.post(CHAT_URL).mock(side_effect=httpx.ConnectError("refused"))

    auth_client.post("/api/v1/classify/run-now")
    email = _email(db_session)
    assert email.status == "classified"
    assert email.classification_id == _category(db_session, "Receipts").id
    assert email.summary is None


@respx.mock
def test_ignore_list_beats_pin(auth_client, db_session, seeded):
    _pin(auth_client, db_session, "Receipts", ["brew.com"])
    auth_client.put("/api/v1/settings", json={"ignore_senders": ["*@brew.com"]})
    auth_client.post("/api/v1/classify/run-now")
    assert _email(db_session).status == "skipped"


@respx.mock
def test_pin_beats_hard_rule_and_feeds_category_rules(auth_client, db_session, seeded):
    from app.models import EmailAction, Rule

    receipts = _category(db_session, "Receipts")
    db_session.add_all([
        Rule(name="hard", priority=10, match_sender_pattern="brew.com", dry_run=True,
             actions=[{"type": "mark_read"}], stop_processing=False),
        Rule(name="receipts", priority=20, match_category_id=receipts.id,
             match_min_confidence=0.8, dry_run=True, actions=[{"type": "archive"}]),
    ])
    db_session.commit()
    _pin(auth_client, db_session, "Receipts", ["brew.com"])
    mock_gmail_full(seeded)
    respx.post(CHAT_URL).mock(return_value=llm_response("s"))

    auth_client.post("/api/v1/classify/run-now")
    email = _email(db_session)
    assert email.rationale == "pinned sender: brew.com"
    planned = {a.action_type for a in db_session.query(EmailAction).filter_by(
        email_id=email.id)}
    assert "archive" in planned  # category rule at confidence 1.0 ≥ 0.8


def test_category_patterns_normalized_and_kept_when_omitted(auth_client, db_session,
                                                            seeded):
    data = _pin(auth_client, db_session, "Receipts", [" a@x.com ", "A@x.com", "", "y.com"])
    assert data["sender_patterns"] == ["a@x.com", "y.com"]

    cat = _category(db_session, "Receipts")
    resp = auth_client.put(f"/api/v1/categories/{cat.id}",
                           json={"name": cat.name, "criteria_md": "changed"})
    assert resp.json()["sender_patterns"] == ["a@x.com", "y.com"]
    assert resp.json()["criteria_version"] == 2

    # pin-only edits don't bump the criteria version
    assert _pin(auth_client, db_session, "Receipts", ["y.com"])["criteria_version"] == 2


def test_duplicate_pin_across_categories_conflicts(auth_client, db_session, seeded):
    _pin(auth_client, db_session, "Receipts", ["shop.com"])
    cat = _category(db_session, "MarketNews")
    resp = auth_client.put(f"/api/v1/categories/{cat.id}", json={
        "name": cat.name, "criteria_md": cat.criteria_md, "sender_patterns": ["SHOP.com"]})
    assert resp.status_code == 409
    assert "Receipts" in resp.json()["detail"]
    resp = auth_client.post("/api/v1/categories", json={
        "name": "New", "sender_patterns": ["shop.com"]})
    assert resp.status_code == 409


def test_feedback_pin_sender_pins_moves_and_requeues(auth_client, db_session, seeded,
                                                     monkeypatch):
    from app.models import Email, Feedback
    from app.services import feedback_service

    scheduled = []
    monkeypatch.setattr(feedback_service, "schedule_proposal_generation",
                        lambda *a, **k: scheduled.append(a))
    _pin(auth_client, db_session, "MarketNews", ["crew@brew.com", "other.com"])
    email = db_session.query(Email).one()
    email.status = "classified"
    email.classification_id = _category(db_session, "MarketNews").id
    db_session.commit()

    receipts = _category(db_session, "Receipts")
    resp = auth_client.post(f"/api/v1/emails/{email.id}/feedback",
                            json={"correct_category_id": receipts.id, "pin_sender": True})
    assert resp.status_code == 201, resp.text

    db_session.expire_all()
    assert _category(db_session, "Receipts").sender_patterns == ["crew@brew.com"]
    assert _category(db_session, "MarketNews").sender_patterns == ["other.com"]
    fb = db_session.query(Feedback).one()
    assert fb.status == "incorporated"
    assert _email(db_session).status == "pending"
    assert scheduled == []  # no criteria-revision proposal


def test_feedback_pin_sender_requires_category(auth_client, db_session, seeded):
    from app.models import Email
    email = db_session.query(Email).one()
    resp = auth_client.post(f"/api/v1/emails/{email.id}/feedback",
                            json={"correct_category_id": None, "pin_sender": True})
    assert resp.status_code == 400
