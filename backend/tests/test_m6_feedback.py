"""M6 acceptance: misclassify -> feedback -> proposal (mock LLM) -> approve
bumps criteria_version + history; reject leaves criteria untouched; stats."""

import json
from datetime import UTC, datetime

import pytest
import respx

from app.models import Category, Email
from tests.test_m2_classification import CHAT_URL, llm_response


@pytest.fixture()
def misclassified(auth_client, db_session):
    market = Category(name="MarketNews", criteria_md="Market commentary.")
    receipts = Category(name="Receipts", criteria_md="Order confirmations.")
    db_session.add_all([market, receipts])
    db_session.flush()
    email = Email(gmail_message_id="f1", sender="shop@store.com",
                  subject="Your order #123", snippet="Thanks for your order",
                  status="classified", classification_id=market.id,
                  confidence=0.7, rationale="Mentions numbers.",
                  received_at=datetime.now(UTC))
    db_session.add(email)
    db_session.commit()
    return {"market": market.id, "receipts": receipts.id, "email": email.id}


def proposal_response(criteria="Order confirmations, invoices, and shipping notices.",
                      explanation="Added shipping notices."):
    return llm_response({"criteria_md": criteria, "explanation": explanation})


def test_feedback_creates_open_row(auth_client, misclassified):
    resp = auth_client.post(f"/api/v1/emails/{misclassified['email']}/feedback", json={
        "correct_category_id": misclassified["receipts"],
        "user_note": "This is a purchase receipt."})
    assert resp.status_code == 201
    fb = resp.json()
    assert fb["status"] == "open"
    assert fb["proposal_status"] == "none"
    assert fb["original_category"] == "MarketNews"
    assert fb["correct_category"] == "Receipts"

    listed = auth_client.get("/api/v1/feedback?status=open").json()
    assert len(listed) == 1


@respx.mock
def test_proposal_generation_targets_correct_category(auth_client, db_session,
                                                      misclassified):
    fb = auth_client.post(f"/api/v1/emails/{misclassified['email']}/feedback", json={
        "correct_category_id": misclassified["receipts"],
        "user_note": "Receipt."}).json()

    chat = respx.post(CHAT_URL).mock(return_value=proposal_response())
    result = auth_client.post(f"/api/v1/feedback/{fb['id']}/generate-proposal").json()
    assert result["proposal_status"] == "pending_review"
    assert "shipping notices" in result["proposed_criteria_md"]
    assert result["proposal_explanation"] == "Added shipping notices."

    request = json.loads(chat.calls[0].request.content)
    system = request["messages"][0]["content"]
    user = request["messages"][1]["content"]
    assert '"Receipts"' in system            # revises the CORRECT category
    assert "Order confirmations." in user    # current criteria included
    assert "Your order #123" in user         # misclassified email included
    assert "Mentions numbers." in user       # original rationale included
    assert "Receipt." in user                # user note included


@respx.mock
def test_correct_none_targets_original_category(auth_client, misclassified):
    fb = auth_client.post(f"/api/v1/emails/{misclassified['email']}/feedback", json={
        "correct_category_id": None, "user_note": "Not market news at all."}).json()
    chat = respx.post(CHAT_URL).mock(return_value=proposal_response("Tighter criteria."))
    auth_client.post(f"/api/v1/feedback/{fb['id']}/generate-proposal")
    system = json.loads(chat.calls[0].request.content)["messages"][0]["content"]
    assert '"MarketNews"' in system          # tightens the wrongly-assigned category


@respx.mock
def test_approve_bumps_version_and_history(auth_client, db_session, misclassified):
    fb = auth_client.post(f"/api/v1/emails/{misclassified['email']}/feedback", json={
        "correct_category_id": misclassified["receipts"]}).json()
    respx.post(CHAT_URL).mock(return_value=proposal_response())
    auth_client.post(f"/api/v1/feedback/{fb['id']}/generate-proposal")

    result = auth_client.post(f"/api/v1/feedback/{fb['id']}/approve").json()
    assert result["criteria_version"] == 2
    assert result["feedback"]["status"] == "incorporated"
    assert result["feedback"]["proposal_status"] == "approved"

    db_session.expire_all()
    receipts = db_session.get(Category, misclassified["receipts"])
    assert receipts.criteria_md.startswith("Order confirmations, invoices")
    assert receipts.criteria_version == 2

    history = auth_client.get(
        f"/api/v1/categories/{misclassified['receipts']}/criteria-history").json()
    assert history[0]["version"] == 2
    assert history[0]["source"] == "llm_feedback"
    assert history[0]["feedback_ids"] == [fb["id"]]


@respx.mock
def test_edit_then_approve_uses_edited_text(auth_client, db_session, misclassified):
    fb = auth_client.post(f"/api/v1/emails/{misclassified['email']}/feedback", json={
        "correct_category_id": misclassified["receipts"]}).json()
    respx.post(CHAT_URL).mock(return_value=proposal_response())
    auth_client.post(f"/api/v1/feedback/{fb['id']}/generate-proposal")

    auth_client.post(f"/api/v1/feedback/{fb['id']}/approve",
                     json={"criteria_md": "My hand-tuned criteria."})
    db_session.expire_all()
    assert db_session.get(Category,
                          misclassified["receipts"]).criteria_md == \
        "My hand-tuned criteria."


@respx.mock
def test_reject_leaves_criteria_untouched(auth_client, db_session, misclassified):
    fb = auth_client.post(f"/api/v1/emails/{misclassified['email']}/feedback", json={
        "correct_category_id": misclassified["receipts"]}).json()
    respx.post(CHAT_URL).mock(return_value=proposal_response())
    auth_client.post(f"/api/v1/feedback/{fb['id']}/generate-proposal")

    result = auth_client.post(f"/api/v1/feedback/{fb['id']}/reject").json()
    assert result["proposal_status"] == "rejected"
    assert result["status"] == "open"        # still resolvable manually

    db_session.expire_all()
    receipts = db_session.get(Category, misclassified["receipts"])
    assert receipts.criteria_md == "Order confirmations."
    assert receipts.criteria_version == 1


def test_approve_without_proposal_409(auth_client, misclassified):
    fb = auth_client.post(f"/api/v1/emails/{misclassified['email']}/feedback", json={
        "correct_category_id": misclassified["receipts"]}).json()
    assert auth_client.post(f"/api/v1/feedback/{fb['id']}/approve").status_code == 409
    assert auth_client.post(f"/api/v1/feedback/{fb['id']}/reject").status_code == 409


def test_precision_stats_reflect_feedback(auth_client, misclassified):
    auth_client.post(f"/api/v1/emails/{misclassified['email']}/feedback", json={
        "correct_category_id": misclassified["receipts"]})
    stats = auth_client.get("/api/v1/stats").json()
    by_name = {p["category"]: p for p in stats["category_precision"]}
    assert by_name["MarketNews"]["classified_7d"] == 1
    assert by_name["MarketNews"]["flagged_wrong_7d"] == 1
    assert by_name["MarketNews"]["precision_7d"] == 0.0
    assert by_name["Receipts"]["flagged_wrong_7d"] == 0
    assert by_name["Receipts"]["precision_7d"] is None  # nothing classified yet


@respx.mock
def test_invalid_proposal_output_502(auth_client, misclassified):
    fb = auth_client.post(f"/api/v1/emails/{misclassified['email']}/feedback", json={
        "correct_category_id": misclassified["receipts"]}).json()
    respx.post(CHAT_URL).mock(return_value=llm_response("not json at all"))
    resp = auth_client.post(f"/api/v1/feedback/{fb['id']}/generate-proposal")
    assert resp.status_code == 502
    assert auth_client.get("/api/v1/feedback?status=open").json()[0][
        "proposal_status"] == "none"


@pytest.fixture()
def two_misclassified(auth_client, db_session):
    """Two emails wrongly classified as MarketNews; correct = Receipts."""
    market = Category(name="MarketNews", criteria_md="Market commentary.")
    receipts = Category(name="Receipts", criteria_md="Order confirmations.")
    db_session.add_all([market, receipts])
    db_session.flush()
    e1 = Email(gmail_message_id="g1", sender="shop@a.com", subject="Order #1",
               snippet="order one", status="classified", classification_id=market.id,
               confidence=0.6, rationale="r1", received_at=datetime.now(UTC))
    e2 = Email(gmail_message_id="g2", sender="shop@b.com", subject="Invoice #2",
               snippet="invoice two", status="classified", classification_id=market.id,
               confidence=0.6, rationale="r2", received_at=datetime.now(UTC))
    db_session.add_all([e1, e2])
    db_session.commit()
    return {"market": market.id, "receipts": receipts.id, "e1": e1.id, "e2": e2.id}


@respx.mock
def test_multiple_feedback_consolidated_into_one_proposal(auth_client, db_session,
                                                          two_misclassified):
    chat = respx.post(CHAT_URL).mock(return_value=proposal_response())
    fb1 = auth_client.post(f"/api/v1/emails/{two_misclassified['e1']}/feedback", json={
        "correct_category_id": two_misclassified["receipts"], "user_note": "note one"}).json()
    fb2 = auth_client.post(f"/api/v1/emails/{two_misclassified['e2']}/feedback", json={
        "correct_category_id": two_misclassified["receipts"], "user_note": "note two"}).json()

    # Generate once -> a single consolidated proposal covering BOTH feedbacks.
    rep = auth_client.post(f"/api/v1/feedback/{fb1['id']}/generate-proposal").json()
    assert rep["proposal_status"] == "pending_review"
    assert sorted(rep["proposal_feedback_ids"]) == sorted([fb1["id"], fb2["id"]])
    assert rep["covers_count"] == 2
    # the most-recent feedback is the representative
    assert rep["id"] == fb2["id"]

    # the prompt mentions BOTH emails and BOTH notes
    user = json.loads(chat.calls[0].request.content)["messages"][1]["content"]
    assert "Order #1" in user and "Invoice #2" in user
    assert "note one" in user and "note two" in user

    # exactly one pending_review row across the queue; fb1 is merged into fb2
    listed = auth_client.get("/api/v1/feedback?status=open").json()
    pending = [f for f in listed if f["proposal_status"] == "pending_review"]
    assert len(pending) == 1
    merged = next(f for f in listed if f["id"] == fb1["id"])
    assert merged["merged_into"] == fb2["id"]


@respx.mock
def test_approve_consolidated_incorporates_all(auth_client, db_session,
                                               two_misclassified):
    respx.post(CHAT_URL).mock(return_value=proposal_response())
    fb1 = auth_client.post(f"/api/v1/emails/{two_misclassified['e1']}/feedback", json={
        "correct_category_id": two_misclassified["receipts"]}).json()
    fb2 = auth_client.post(f"/api/v1/emails/{two_misclassified['e2']}/feedback", json={
        "correct_category_id": two_misclassified["receipts"]}).json()
    rep = auth_client.post(f"/api/v1/feedback/{fb1['id']}/generate-proposal").json()

    result = auth_client.post(f"/api/v1/feedback/{rep['id']}/approve").json()
    assert result["criteria_version"] == 2  # bumped once

    # both feedbacks incorporated; history records both ids
    db_session.expire_all()
    from app.models import Feedback
    statuses = {f.id: f.status for f in db_session.query(Feedback).all()}
    assert statuses[fb1["id"]] == "incorporated"
    assert statuses[fb2["id"]] == "incorporated"
    history = auth_client.get(
        f"/api/v1/categories/{two_misclassified['receipts']}/criteria-history").json()
    assert sorted(history[0]["feedback_ids"]) == sorted([fb1["id"], fb2["id"]])
    assert auth_client.get("/api/v1/feedback?status=open").json() == []


@respx.mock
def test_new_feedback_regenerates_to_include_it(auth_client, db_session,
                                                two_misclassified):
    respx.post(CHAT_URL).mock(return_value=proposal_response())
    fb1 = auth_client.post(f"/api/v1/emails/{two_misclassified['e1']}/feedback", json={
        "correct_category_id": two_misclassified["receipts"]}).json()
    auth_client.post(f"/api/v1/feedback/{fb1['id']}/generate-proposal")
    # second feedback arrives, then regenerate (debounce is async; trigger manually)
    fb2 = auth_client.post(f"/api/v1/emails/{two_misclassified['e2']}/feedback", json={
        "correct_category_id": two_misclassified["receipts"]}).json()
    rep = auth_client.post(f"/api/v1/feedback/{fb2['id']}/generate-proposal").json()

    assert sorted(rep["proposal_feedback_ids"]) == sorted([fb1["id"], fb2["id"]])
    # the old representative (fb1) is no longer pending — superseded
    listed = auth_client.get("/api/v1/feedback?status=open").json()
    pending = [f for f in listed if f["proposal_status"] == "pending_review"]
    assert len(pending) == 1 and pending[0]["id"] == fb2["id"]
