"""M6 acceptance: misclassify -> feedback -> proposal (mock LLM) -> approve
bumps criteria_version + history; reject leaves criteria untouched; stats."""

import json
from datetime import UTC, datetime

import pytest
import respx

from app.models import Category, CategoryCriteriaHistory, Email, Feedback
from app.services import feedback_service
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


def exclusion_response(criteria="School criteria, excluding Peachjar flyer notifications.",
                       explanation="Excluded Peachjar."):
    return llm_response({"criteria_md": criteria, "explanation": explanation})


@pytest.fixture()
def peachjar_source_feedback(db_session):
    """An email wrongly classified as 'School'; the user corrects it to 'Ads'
    and names Peachjar as the concrete cause. `source_category_id` is set
    directly on the DB row here — the route layer doesn't wire this up until
    a later task, per the brief."""
    school = Category(name="School", criteria_md="School announcements and forms.")
    ads = Category(name="Ads", criteria_md="Promotional and marketing email.")
    db_session.add_all([school, ads])
    db_session.flush()
    email = Email(gmail_message_id="p1", sender="noreply@peachjar.com",
                  subject="New flyer from your school", snippet="Check out this flyer",
                  status="classified", classification_id=school.id,
                  confidence=0.65, rationale="Mentions school.",
                  received_at=datetime.now(UTC))
    db_session.add(email)
    db_session.flush()
    fb = Feedback(email_id=email.id, correct_category_id=ads.id,
                  source_category_id=school.id,
                  user_note="This is peachjar, an ad platform. peachjar again.")
    db_session.add(fb)
    db_session.commit()
    return {"school": school.id, "ads": ads.id, "email": email.id, "feedback": fb.id}


@respx.mock
async def test_exclusion_proposal_generation_mentions_concrete_pattern(
        db_session, peachjar_source_feedback):
    respx.post(CHAT_URL).mock(return_value=exclusion_response())
    representative = await feedback_service.generate_exclusion_proposal_for_category(
        db_session, peachjar_source_feedback["school"])
    assert representative is not None
    assert representative.proposal_source_status == "pending_review"
    assert "Peachjar" in representative.proposed_source_criteria_md
    assert representative.proposal_source_explanation == "Excluded Peachjar."
    assert representative.proposal_source_feedback_ids == [
        peachjar_source_feedback["feedback"]]


@respx.mock
async def test_approve_source_proposal_bumps_source_category_leaves_feedback_open(
        db_session, peachjar_source_feedback):
    respx.post(CHAT_URL).mock(return_value=exclusion_response())
    representative = await feedback_service.generate_exclusion_proposal_for_category(
        db_session, peachjar_source_feedback["school"])

    category = feedback_service.approve_proposal(
        db_session, representative, kind="source")
    assert category.id == peachjar_source_feedback["school"]
    assert category.criteria_version == 2
    assert "Peachjar" in category.criteria_md

    db_session.expire_all()
    school = db_session.get(Category, peachjar_source_feedback["school"])
    assert school.criteria_version == 2

    history = db_session.query(CategoryCriteriaHistory).filter_by(
        category_id=peachjar_source_feedback["school"]).all()
    assert len(history) == 1
    assert history[0].version == 2
    assert history[0].source == "llm_feedback"
    assert history[0].feedback_ids == [peachjar_source_feedback["feedback"]]

    fb = db_session.get(Feedback, peachjar_source_feedback["feedback"])
    assert fb.status == "open"          # only target approval resolves feedback
    assert fb.proposal_source_status == "approved"


@respx.mock
async def test_reject_source_proposal_leaves_criteria_untouched(
        db_session, peachjar_source_feedback):
    respx.post(CHAT_URL).mock(return_value=exclusion_response())
    representative = await feedback_service.generate_exclusion_proposal_for_category(
        db_session, peachjar_source_feedback["school"])

    feedback_service.reject_proposal(db_session, representative, kind="source")

    db_session.expire_all()
    school = db_session.get(Category, peachjar_source_feedback["school"])
    assert school.criteria_md == "School announcements and forms."
    assert school.criteria_version == 1

    fb = db_session.get(Feedback, peachjar_source_feedback["feedback"])
    assert fb.proposal_source_status == "rejected"
    assert fb.status == "open"


# --- Task 3: API routes wiring ---------------------------------------------


def test_create_feedback_sets_source_category_id(auth_client, misclassified):
    resp = auth_client.post(f"/api/v1/emails/{misclassified['email']}/feedback", json={
        "correct_category_id": misclassified["receipts"]})
    assert resp.status_code == 201
    fb = resp.json()
    assert fb["source_category_id"] == misclassified["market"]
    assert fb["source_category"] == "MarketNews"


def test_create_feedback_correct_none_leaves_source_category_unset(auth_client,
                                                                    misclassified):
    resp = auth_client.post(f"/api/v1/emails/{misclassified['email']}/feedback", json={
        "correct_category_id": None})
    fb = resp.json()
    assert fb["source_category_id"] is None
    assert fb["source_category"] is None


def test_generate_source_proposal_400_without_source_category(auth_client, misclassified):
    fb = auth_client.post(f"/api/v1/emails/{misclassified['email']}/feedback", json={
        "correct_category_id": None}).json()
    resp = auth_client.post(f"/api/v1/feedback/{fb['id']}/generate-source-proposal")
    assert resp.status_code == 400


@respx.mock
def test_approve_source_via_route_leaves_feedback_open(auth_client, db_session,
                                                        peachjar_source_feedback):
    respx.post(CHAT_URL).mock(return_value=exclusion_response())
    auth_client.post(
        f"/api/v1/feedback/{peachjar_source_feedback['feedback']}/generate-source-proposal")

    result = auth_client.post(
        f"/api/v1/feedback/{peachjar_source_feedback['feedback']}/approve",
        json={"kind": "source"}).json()
    assert result["criteria_version"] == 2
    assert result["feedback"]["status"] == "open"                 # not incorporated
    assert result["feedback"]["proposal_source_status"] == "approved"
    assert result["feedback"]["proposal_status"] == "none"        # target side untouched

    db_session.expire_all()
    school = db_session.get(Category, peachjar_source_feedback["school"])
    assert school.criteria_version == 2
    assert "Peachjar" in school.criteria_md


@respx.mock
def test_reject_source_via_route_leaves_criteria_untouched(auth_client, db_session,
                                                            peachjar_source_feedback):
    respx.post(CHAT_URL).mock(return_value=exclusion_response())
    auth_client.post(
        f"/api/v1/feedback/{peachjar_source_feedback['feedback']}/generate-source-proposal")

    result = auth_client.post(
        f"/api/v1/feedback/{peachjar_source_feedback['feedback']}/reject",
        json={"kind": "source"}).json()
    assert result["proposal_source_status"] == "rejected"
    assert result["status"] == "open"

    db_session.expire_all()
    school = db_session.get(Category, peachjar_source_feedback["school"])
    assert school.criteria_md == "School announcements and forms."
    assert school.criteria_version == 1


@respx.mock
def test_approve_target_409_when_only_source_proposal_pending(auth_client,
                                                               peachjar_source_feedback):
    """kind defaults to 'target', so a pending SOURCE-only proposal must not
    satisfy the approve/reject pending-review check for the default kind."""
    respx.post(CHAT_URL).mock(return_value=exclusion_response())
    auth_client.post(
        f"/api/v1/feedback/{peachjar_source_feedback['feedback']}/generate-source-proposal")

    assert auth_client.post(
        f"/api/v1/feedback/{peachjar_source_feedback['feedback']}/approve"
    ).status_code == 409
    assert auth_client.post(
        f"/api/v1/feedback/{peachjar_source_feedback['feedback']}/reject"
    ).status_code == 409


@pytest.fixture()
def both_pending_feedback(db_session):
    """A single feedback row with BOTH a pending target proposal (School ->
    Ads) and a pending source proposal (narrowing School) already stored
    directly, so tests can approve either side first without depending on
    LLM-driven generation."""
    school = Category(name="School", criteria_md="School announcements and forms.")
    ads = Category(name="Ads", criteria_md="Promotional and marketing email.")
    db_session.add_all([school, ads])
    db_session.flush()
    email = Email(gmail_message_id="both1", sender="noreply@peachjar.com",
                  subject="New flyer from your school", snippet="Check out this flyer",
                  status="classified", classification_id=school.id,
                  confidence=0.65, rationale="Mentions school.",
                  received_at=datetime.now(UTC))
    db_session.add(email)
    db_session.flush()
    fb = Feedback(email_id=email.id, correct_category_id=ads.id,
                  source_category_id=school.id,
                  user_note="This is peachjar, an ad platform.",
                  proposed_criteria_md="Promotional and marketing email, incl. Peachjar.",
                  proposal_explanation="Broadened Ads.",
                  proposal_status="pending_review",
                  proposed_source_criteria_md="School criteria, excluding Peachjar.",
                  proposal_source_explanation="Excluded Peachjar.",
                  proposal_source_status="pending_review")
    db_session.add(fb)
    db_session.flush()
    fb.proposal_feedback_ids = [fb.id]
    fb.proposal_source_feedback_ids = [fb.id]
    db_session.commit()
    return {"school": school.id, "ads": ads.id, "email": email.id, "feedback": fb.id}


def test_approve_target_first_leaves_source_reachable(auth_client, db_session,
                                                       both_pending_feedback):
    """Critical fix: approving the TARGET proposal must not orphan a still-
    pending SOURCE proposal — the feedback row must stay 'open' and remain
    visible to source-proposal machinery."""
    fb_id = both_pending_feedback["feedback"]
    result = auth_client.post(f"/api/v1/feedback/{fb_id}/approve").json()
    assert result["feedback"]["status"] == "open"           # NOT incorporated
    assert result["feedback"]["proposal_status"] == "approved"

    db_session.expire_all()
    fb = db_session.get(Feedback, fb_id)
    assert fb.status == "open"
    assert fb.resolved_at is None

    # still reachable for source-proposal purposes
    open_source = feedback_service.open_feedback_for_source_category(
        db_session, both_pending_feedback["school"])
    assert fb_id in [f.id for f in open_source]
    listed = auth_client.get("/api/v1/feedback?status=open").json()
    assert fb_id in [f["id"] for f in listed]


def test_approve_source_then_target_incorporates(auth_client, db_session,
                                                  both_pending_feedback):
    """Approving SOURCE first leaves the row open (existing/locked-in
    behavior); approving TARGET afterward completes the deferred resolution
    and incorporates it."""
    fb_id = both_pending_feedback["feedback"]

    result = auth_client.post(f"/api/v1/feedback/{fb_id}/approve",
                              json={"kind": "source"}).json()
    assert result["feedback"]["status"] == "open"
    db_session.expire_all()
    fb = db_session.get(Feedback, fb_id)
    assert fb.status == "open"
    assert fb.resolved_at is None

    result = auth_client.post(f"/api/v1/feedback/{fb_id}/approve").json()
    assert result["feedback"]["status"] == "incorporated"

    db_session.expire_all()
    fb = db_session.get(Feedback, fb_id)
    assert fb.status == "incorporated"
    assert fb.resolved_at is not None


def test_approve_both_orders_incorporate_once_no_clobber(auth_client, db_session,
                                                          both_pending_feedback):
    """Source-then-target (or vice versa): feedback ends incorporated exactly
    once, and BOTH categories end up bumped exactly one version each (neither
    edit clobbers/reverts the other)."""
    fb_id = both_pending_feedback["feedback"]
    auth_client.post(f"/api/v1/feedback/{fb_id}/approve", json={"kind": "source"})
    result = auth_client.post(f"/api/v1/feedback/{fb_id}/approve").json()
    assert result["feedback"]["status"] == "incorporated"

    db_session.expire_all()
    school = db_session.get(Category, both_pending_feedback["school"])
    ads = db_session.get(Category, both_pending_feedback["ads"])
    assert school.criteria_version == 2
    assert "Peachjar" in school.criteria_md
    assert ads.criteria_version == 2
    assert "Peachjar" in ads.criteria_md

    fb = db_session.get(Feedback, fb_id)
    assert fb.status == "incorporated"
    # incorporated exactly once — resolved_at set, no double-processing artifacts
    assert fb.resolved_at is not None


@respx.mock
def test_approve_target_resets_stale_source_proposal_same_category(
        auth_client, db_session):
    """Overwrite-prevention fix: category X (Ads) has a pending TARGET
    proposal (feedback A: -> Ads) and, independently, a pending SOURCE
    proposal targeting Ads too (feedback B: Ads -> Personal, i.e. Ads is B's
    *source* category). Approving A's target proposal for Ads must reset B's
    stale source proposal rather than let it silently overwrite Ads' new
    criteria if approved later."""
    ads = Category(name="Ads", criteria_md="Promotional and marketing email.")
    personal = Category(name="Personal", criteria_md="Personal correspondence.")
    db_session.add_all([ads, personal])
    db_session.flush()

    email_a = Email(gmail_message_id="collideA", sender="shop@a.com",
                    subject="Deal", snippet="deal", status="classified",
                    classification_id=None, confidence=0.5, rationale="r",
                    received_at=datetime.now(UTC))
    email_b = Email(gmail_message_id="collideB", sender="shop@b.com",
                    subject="Newsletter", snippet="news", status="classified",
                    classification_id=ads.id, confidence=0.5, rationale="r",
                    received_at=datetime.now(UTC))
    db_session.add_all([email_a, email_b])
    db_session.commit()

    fb_a = auth_client.post(f"/api/v1/emails/{email_a.id}/feedback", json={
        "correct_category_id": ads.id}).json()
    fb_b = auth_client.post(f"/api/v1/emails/{email_b.id}/feedback", json={
        "correct_category_id": personal.id}).json()

    respx.post(CHAT_URL).mock(return_value=proposal_response(
        criteria="Promotional and marketing email, broadened.",
        explanation="Broadened Ads."))
    auth_client.post(f"/api/v1/feedback/{fb_a['id']}/generate-proposal")

    respx.post(CHAT_URL).mock(return_value=exclusion_response(
        criteria="Promotional and marketing email, excluding shop@b.com newsletters.",
        explanation="Excluded shop@b.com."))
    auth_client.post(f"/api/v1/feedback/{fb_b['id']}/generate-source-proposal")

    db_session.expire_all()
    fb_b_row = db_session.get(Feedback, fb_b["id"])
    assert fb_b_row.proposal_source_status == "pending_review"

    # Approve A's target proposal for Ads.
    auth_client.post(f"/api/v1/feedback/{fb_a['id']}/approve")

    db_session.expire_all()
    ads_after = db_session.get(Category, ads.id)
    assert ads_after.criteria_version == 2
    assert "broadened" in ads_after.criteria_md

    # B's stale source proposal for Ads must be reset, not left approvable.
    fb_b_row = db_session.get(Feedback, fb_b["id"])
    assert fb_b_row.proposal_source_status == "none"
    assert fb_b_row.proposed_source_criteria_md is None
    assert fb_b_row.proposal_source_explanation is None
    assert fb_b_row.proposal_source_feedback_ids is None

    # Regenerating B's source proposal reflects Ads' NEW criteria/version.
    respx.post(CHAT_URL).mock(return_value=exclusion_response(
        criteria="Promotional and marketing email, broadened, excluding shop@b.com.",
        explanation="Excluded shop@b.com from broadened criteria."))
    regen = auth_client.post(
        f"/api/v1/feedback/{fb_b['id']}/generate-source-proposal").json()
    assert regen["proposal_source_status"] == "pending_review"
    assert "broadened" in regen["proposed_source_criteria_md"]


@respx.mock
def test_source_merged_into_shown_in_list(auth_client, db_session):
    """Two feedback rows sharing a source category are consolidated into one
    exclusion proposal; the non-representative row is marked via
    `source_merged_into`, mirroring the existing target-side `merged_into`."""
    school = Category(name="School", criteria_md="School announcements and forms.")
    ads = Category(name="Ads", criteria_md="Promotional and marketing email.")
    personal = Category(name="Personal", criteria_md="Personal correspondence.")
    db_session.add_all([school, ads, personal])
    db_session.flush()
    e1 = Email(gmail_message_id="s1", sender="a@peachjar.com", subject="Flyer 1",
              snippet="flyer", status="classified", classification_id=school.id,
              confidence=0.6, rationale="r1", received_at=datetime.now(UTC))
    e2 = Email(gmail_message_id="s2", sender="b@peachjar.com", subject="Flyer 2",
              snippet="flyer2", status="classified", classification_id=school.id,
              confidence=0.6, rationale="r2", received_at=datetime.now(UTC))
    db_session.add_all([e1, e2])
    db_session.commit()

    fb1 = auth_client.post(f"/api/v1/emails/{e1.id}/feedback", json={
        "correct_category_id": ads.id}).json()
    fb2 = auth_client.post(f"/api/v1/emails/{e2.id}/feedback", json={
        "correct_category_id": personal.id}).json()

    respx.post(CHAT_URL).mock(return_value=exclusion_response())
    auth_client.post(f"/api/v1/feedback/{fb1['id']}/generate-source-proposal")

    listed = auth_client.get("/api/v1/feedback?status=open").json()
    merged = next(f for f in listed if f["id"] == fb1["id"])
    rep = next(f for f in listed if f["id"] == fb2["id"])
    assert merged["source_merged_into"] == fb2["id"]
    assert rep["source_covers_count"] == 2
