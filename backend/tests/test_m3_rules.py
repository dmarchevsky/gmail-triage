"""M3 acceptance: rule matching matrix, closed action enum, dry-run zero
mutations, live execution, send-path absence."""

import json
import pathlib

import pytest
import respx

from app.models import Email, Rule
from app.services import rules as rules_engine
from tests.test_m1_gmail import CLIENT_SECRET_JSON, b64url, gmail_message, make_token
from tests.test_m2_classification import CHAT_URL, llm_response

GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"


def email_with(classification_id=None, confidence=None, sender="a@b.com", **kw):
    return Email(gmail_message_id=kw.pop("mid", "x1"), sender=sender,
                 classification_id=classification_id, confidence=confidence,
                 status="classified", **kw)


def rule_with(**kw):
    defaults = dict(id=1, name="r", enabled=True, priority=100,
                    match_category_id=None, match_min_confidence=0.0,
                    match_sender_pattern=None, dry_run=True,
                    actions=[{"type": "mark_read"}], stop_processing=True)
    defaults.update(kw)
    return Rule(**defaults)


# ── Matching matrix (unit) ───────────────────────────────────────────────────

class TestRuleMatching:
    def test_category_gate(self):
        rule = rule_with(match_category_id=7)
        assert rules_engine.rule_matches(rule, email_with(classification_id=7,
                                                          confidence=0.9))
        assert not rules_engine.rule_matches(rule, email_with(classification_id=8,
                                                              confidence=0.9))
        assert not rules_engine.rule_matches(rule, email_with(classification_id=None,
                                                              confidence=0.9))

    def test_no_category_matches_any(self):
        rule = rule_with(match_category_id=None)
        assert rules_engine.rule_matches(rule, email_with(classification_id=42,
                                                          confidence=0.5))
        assert rules_engine.rule_matches(rule, email_with(classification_id=None,
                                                          confidence=0.5))

    def test_confidence_gate(self):
        rule = rule_with(match_min_confidence=0.8)
        assert rules_engine.rule_matches(rule, email_with(confidence=0.8))
        assert not rules_engine.rule_matches(rule, email_with(confidence=0.79))
        assert not rules_engine.rule_matches(rule, email_with(confidence=None))

    def test_sender_gate(self):
        rule = rule_with(match_sender_pattern="*@spam.io")
        assert rules_engine.rule_matches(rule, email_with(sender="X <x@spam.io>",
                                                          confidence=1.0))
        assert not rules_engine.rule_matches(rule, email_with(sender="x@ok.io",
                                                              confidence=1.0))

    def test_disabled_never_matches(self):
        rule = rule_with(enabled=False)
        assert not rules_engine.rule_matches(rule, email_with(confidence=1.0))

    def test_priority_and_stop_processing(self):
        first = rule_with(id=1, priority=10, actions=[{"type": "mark_read"}],
                          stop_processing=True)
        second = rule_with(id=2, priority=20, actions=[{"type": "archive"}])
        email = email_with(confidence=0.9)
        planned = rules_engine.evaluate_rules([second, first], email)
        assert [(r.id, a["type"]) for r, a in planned] == [(1, "mark_read")]

    def test_fallthrough_when_stop_false(self):
        first = rule_with(id=1, priority=10, actions=[{"type": "mark_read"}],
                          stop_processing=False)
        second = rule_with(id=2, priority=20, actions=[{"type": "archive"}])
        planned = rules_engine.evaluate_rules([first, second], email_with(confidence=0.9))
        assert [(r.id, a["type"]) for r, a in planned] == [(1, "mark_read"),
                                                           (2, "archive")]

    def test_non_matching_skipped_in_order(self):
        gated = rule_with(id=1, priority=10, match_min_confidence=0.99)
        catchall = rule_with(id=2, priority=20, actions=[{"type": "archive"}])
        planned = rules_engine.evaluate_rules([gated, catchall],
                                              email_with(confidence=0.5))
        assert [r.id for r, _ in planned] == [2]

    def test_no_rule_matched_means_no_actions(self):
        rule = rule_with(match_min_confidence=0.99)
        assert rules_engine.evaluate_rules([rule], email_with(confidence=0.1)) == []


# ── Action enum is closed ────────────────────────────────────────────────────

def test_action_enum_closed_set(auth_client):
    for bad in ["send", "reply", "forward", "draft", "delete", "permanent_delete"]:
        resp = auth_client.post("/api/v1/rules", json={
            "name": "evil", "actions": [{"type": bad}]})
        assert resp.status_code == 422, f"action {bad!r} must be rejected"
    resp = auth_client.post("/api/v1/rules", json={
        "name": "bad-params", "actions": [{"type": "remove_label"}]})
    assert resp.status_code == 422  # remove_label requires label_id


def test_no_send_capable_code_paths():
    """Spec M3 accept: grep for absence of send/draft Gmail usage."""
    app_dir = pathlib.Path(__file__).resolve().parent.parent / "app"
    offenders = []
    for path in app_dir.rglob("*.py"):
        text = path.read_text()
        for needle in ["messages/send", "/drafts", "messages.send", "drafts.create",
                       "messages/import", "messages/insert", "users.drafts"]:
            if needle in text:
                offenders.append(f"{path.name}: {needle}")
    assert offenders == []


# ── Pipeline integration ─────────────────────────────────────────────────────

@pytest.fixture()
def pipeline(auth_client, db_session):
    """Connected Gmail + 1 category + pending email + full-message mock data."""
    from app.services import gmail, settings_service

    settings_service.set_setting(db_session, "gmail_client_secret_json",
                                 CLIENT_SECRET_JSON)
    gmail.save_token(db_session, make_token(), email="me@gmail.test")
    db_session.commit()

    cat = auth_client.post("/api/v1/categories", json={
        "name": "MarketNews", "criteria_md": "Market stuff."}).json()

    from app.models import Label
    label = Label(name="MailTriage/MarketNews")
    db_session.add(label)
    db_session.add(Email(gmail_message_id="m1", sender="Brew <crew@brew.com>",
                         sender_domain="brew.com", subject="Stocks slide",
                         snippet="Futures fell", status="pending"))
    db_session.commit()
    full = gmail_message("m1")
    full["payload"]["parts"] = [
        {"mimeType": "text/plain", "body": {"data": b64url("Body.")}}]
    return {"category": cat, "label_id": label.id, "full": full}


def classify_ok(category="MarketNews", confidence=0.9):
    return llm_response({"category": category, "confidence": confidence,
                         "rationale": "r"})


@respx.mock
def test_dry_run_records_actions_no_gmail_mutation(auth_client, db_session, pipeline):
    auth_client.post("/api/v1/rules", json={
        "name": "label+archive", "match_category_id": pipeline["category"]["id"],
        "match_min_confidence": 0.8,
        "actions": [{"type": "add_label", "label_id": pipeline["label_id"]},
                    {"type": "mark_read"}, {"type": "archive"}]})

    respx.get(f"{GMAIL_API}/messages/m1").respond(200, json=pipeline["full"])
    respx.post(CHAT_URL).mock(return_value=classify_ok())
    modify = respx.post(f"{GMAIL_API}/messages/m1/modify")
    trash = respx.post(f"{GMAIL_API}/messages/m1/trash")
    labels = respx.get(f"{GMAIL_API}/labels")

    resp = auth_client.post("/api/v1/classify/run-now")
    assert resp.json()["actioned"] == 1

    # ZERO Gmail mutations in dry-run
    assert modify.call_count == 0
    assert trash.call_count == 0
    assert labels.call_count == 0

    from app.models import EmailAction
    actions = db_session.query(EmailAction).all()
    assert [a.action_type for a in actions] == ["add_label", "mark_read", "archive"]
    assert all(a.dry_run and not a.executed for a in actions)
    email = db_session.query(Email).one()
    assert email.status == "actioned"
    assert email.dry_run is True


@respx.mock
def test_live_mode_executes_label_read_archive(auth_client, db_session, pipeline):
    auth_client.post("/api/v1/rules", json={
        "name": "label+read+archive", "match_category_id": pipeline["category"]["id"],
        "dry_run": False,
        "actions": [{"type": "add_label", "label_id": pipeline["label_id"]},
                    {"type": "mark_read"}, {"type": "archive"}]})

    respx.get(f"{GMAIL_API}/messages/m1").respond(200, json=pipeline["full"])
    respx.post(CHAT_URL).mock(return_value=classify_ok())
    respx.get(f"{GMAIL_API}/labels").respond(200, json={"labels": [
        {"id": "Label_1", "name": "Other"}]})
    create_label = respx.post(f"{GMAIL_API}/labels").respond(200, json={
        "id": "Label_2", "name": "MailTriage/MarketNews"})
    modify = respx.post(f"{GMAIL_API}/messages/m1/modify").respond(200, json={})

    auth_client.post("/api/v1/classify/run-now")

    assert create_label.called  # label auto-created
    assert modify.call_count == 1  # batched into one modify call
    payload = json.loads(modify.calls[0].request.content)
    assert payload["addLabelIds"] == ["Label_2"]
    assert sorted(payload["removeLabelIds"]) == ["INBOX", "UNREAD"]

    from app.models import EmailAction
    actions = db_session.query(EmailAction).all()
    assert all(a.executed and not a.dry_run for a in actions)
    assert db_session.query(Email).one().status == "actioned"


@respx.mock
def test_live_mode_trash(auth_client, db_session, pipeline):
    auth_client.post("/api/v1/rules", json={
        "name": "trash spam", "match_category_id": pipeline["category"]["id"],
        "dry_run": False, "actions": [{"type": "trash"}]})

    respx.get(f"{GMAIL_API}/messages/m1").respond(200, json=pipeline["full"])
    respx.post(CHAT_URL).mock(return_value=classify_ok())
    trash = respx.post(f"{GMAIL_API}/messages/m1/trash").respond(200, json={})

    auth_client.post("/api/v1/classify/run-now")
    assert trash.call_count == 1


@respx.mock
def test_hard_rule_bypasses_llm(auth_client, db_session, pipeline):
    auth_client.post("/api/v1/rules", json={
        "name": "hard: brew", "match_sender_pattern": "*@brew.com",
        "actions": [{"type": "mark_read"}]})
    chat = respx.post(CHAT_URL)

    resp = auth_client.post("/api/v1/classify/run-now")
    assert resp.json()["classified"] == 1
    assert chat.call_count == 0  # LLM bypassed

    email = db_session.query(Email).one()
    assert email.confidence == 1.0
    assert "hard rule" in email.rationale
    assert email.status == "actioned"


@respx.mock
def test_action_failure_recorded_not_executed(auth_client, db_session, pipeline):
    from app.services import gmail as gmail_mod
    auth_client.post("/api/v1/rules", json={
        "name": "archive", "dry_run": False, "actions": [{"type": "archive"}]})

    respx.get(f"{GMAIL_API}/messages/m1").respond(200, json=pipeline["full"])
    respx.post(CHAT_URL).mock(return_value=classify_ok())
    respx.post(f"{GMAIL_API}/messages/m1/modify").respond(403, json={"error": "nope"})

    auth_client.post("/api/v1/classify/run-now")

    from app.models import EmailAction
    action = db_session.query(EmailAction).one()
    assert action.executed is False
    assert action.error is not None
    assert gmail_mod is not None


# ── Rules CRUD/reorder/test ──────────────────────────────────────────────────

def test_rules_crud_reorder_and_test(auth_client, db_session):
    r1 = auth_client.post("/api/v1/rules", json={
        "name": "A", "priority": 10, "actions": [{"type": "mark_read"}]}).json()
    r2 = auth_client.post("/api/v1/rules", json={
        "name": "B", "priority": 20, "actions": [{"type": "archive"}],
        "match_min_confidence": 0.5}).json()

    listed = auth_client.get("/api/v1/rules").json()
    assert [r["name"] for r in listed] == ["A", "B"]

    reordered = auth_client.post("/api/v1/rules/reorder", json={
        "ordered_ids": [r2["id"], r1["id"]]}).json()
    assert [r["name"] for r in reordered] == ["B", "A"]

    db_session.add(Email(gmail_message_id="t1", sender="x@y.com", subject="s",
                         status="classified", confidence=0.9))
    db_session.add(Email(gmail_message_id="t2", sender="x@y.com", subject="s2",
                         status="classified", confidence=0.3))
    db_session.commit()

    result = auth_client.post(f"/api/v1/rules/{r2['id']}/test", json={}).json()
    assert result["tested"] == 2
    assert result["matched"] == 1  # only the 0.9-confidence one passes 0.5 gate

    assert auth_client.delete(f"/api/v1/rules/{r1['id']}").status_code == 200
    assert len(auth_client.get("/api/v1/rules").json()) == 1


# ── Per-rule dry-run: mixed mode + apply-planned ─────────────────────────────

@respx.mock
def test_mixed_live_and_dry_rules_on_one_email(auth_client, db_session, pipeline):
    auth_client.post("/api/v1/rules", json={
        "name": "live read", "match_category_id": pipeline["category"]["id"],
        "dry_run": False, "stop_processing": False,
        "actions": [{"type": "mark_read"}]})
    auth_client.post("/api/v1/rules", json={
        "name": "dry archive", "match_category_id": pipeline["category"]["id"],
        "priority": 200, "actions": [{"type": "archive"}]})

    respx.get(f"{GMAIL_API}/messages/m1").respond(200, json=pipeline["full"])
    respx.post(CHAT_URL).mock(return_value=classify_ok())
    modify = respx.post(f"{GMAIL_API}/messages/m1/modify").respond(200, json={})

    auth_client.post("/api/v1/classify/run-now")

    # Live rule executed (mark_read only); dry rule only recorded.
    payload = json.loads(modify.calls[0].request.content)
    assert payload["removeLabelIds"] == ["UNREAD"]

    from app.models import EmailAction
    actions = {a.action_type: a for a in db_session.query(EmailAction).all()}
    assert actions["mark_read"].executed and not actions["mark_read"].dry_run
    assert not actions["archive"].executed and actions["archive"].dry_run
    email = db_session.query(Email).one()
    assert email.dry_run is False  # something ran live


@pytest.fixture()
def dry_planned(auth_client, db_session, pipeline):
    """A dry rule that has recorded plans for one classified email."""
    rule = auth_client.post("/api/v1/rules", json={
        "name": "graduate me", "match_category_id": pipeline["category"]["id"],
        "actions": [{"type": "add_label", "label_id": pipeline["label_id"]},
                    {"type": "mark_read"}]}).json()
    with respx.mock:
        respx.get(f"{GMAIL_API}/messages/m1").respond(200, json=pipeline["full"])
        respx.post(CHAT_URL).mock(return_value=classify_ok())
        auth_client.post("/api/v1/classify/run-now")
    return rule


def test_pending_planned_surfaced(auth_client, db_session, dry_planned):
    rules = auth_client.get("/api/v1/rules").json()
    assert rules[0]["dry_run"] is True
    assert rules[0]["pending_planned"] == 2


def test_apply_planned_requires_live_rule(auth_client, dry_planned):
    resp = auth_client.post(f"/api/v1/rules/{dry_planned['id']}/apply-planned")
    assert resp.status_code == 409
    assert "dry-run" in resp.json()["detail"]


@respx.mock
def test_apply_planned_executes_recorded_plans(auth_client, db_session, dry_planned):
    # Graduate the rule to live.
    auth_client.put(f"/api/v1/rules/{dry_planned['id']}", json={
        **{k: dry_planned[k] for k in
           ["name", "enabled", "priority", "match_category_id",
            "match_min_confidence", "match_sender_pattern", "actions",
            "stop_processing"]},
        "dry_run": False})

    respx.get(f"{GMAIL_API}/labels").respond(200, json={"labels": [
        {"id": "Label_7", "name": "MailTriage/MarketNews"}]})
    modify = respx.post(f"{GMAIL_API}/messages/m1/modify").respond(200, json={})

    resp = auth_client.post(f"/api/v1/rules/{dry_planned['id']}/apply-planned")
    assert resp.status_code == 200
    assert resp.json() == {"applied": 2, "failed": 0, "emails": 1}

    payload = json.loads(modify.calls[0].request.content)
    assert payload["addLabelIds"] == ["Label_7"]
    assert payload["removeLabelIds"] == ["UNREAD"]

    from app.models import EmailAction
    db_session.expire_all()
    actions = db_session.query(EmailAction).all()
    assert all(a.executed and not a.dry_run and a.executed_at for a in actions)
    assert db_session.query(Email).one().dry_run is False

    # Idempotent: nothing left to apply.
    resp = auth_client.post(f"/api/v1/rules/{dry_planned['id']}/apply-planned")
    assert resp.json() == {"applied": 0, "failed": 0, "emails": 0}
    assert auth_client.get("/api/v1/rules").json()[0]["pending_planned"] == 0


@respx.mock
def test_apply_planned_records_failure_and_continues(auth_client, db_session,
                                                     dry_planned, pipeline):
    # Second email with plans from the same rule.
    db_session.add(Email(gmail_message_id="m2", sender="Brew <crew@brew.com>",
                         sender_domain="brew.com", subject="s2", snippet="x",
                         status="pending"))
    db_session.commit()
    full2 = gmail_message("m2")
    full2["payload"]["parts"] = pipeline["full"]["payload"]["parts"]
    respx.get(f"{GMAIL_API}/messages/m2").respond(200, json=full2)
    respx.post(CHAT_URL).mock(return_value=classify_ok())
    auth_client.post("/api/v1/classify/run-now")

    auth_client.put(f"/api/v1/rules/{dry_planned['id']}", json={
        **{k: dry_planned[k] for k in
           ["name", "enabled", "priority", "match_category_id",
            "match_min_confidence", "match_sender_pattern", "actions",
            "stop_processing"]},
        "dry_run": False})

    respx.get(f"{GMAIL_API}/labels").respond(200, json={"labels": [
        {"id": "Label_7", "name": "MailTriage/MarketNews"}]})
    respx.post(f"{GMAIL_API}/messages/m1/modify").respond(403, json={"error": "no"})
    respx.post(f"{GMAIL_API}/messages/m2/modify").respond(200, json={})

    resp = auth_client.post(f"/api/v1/rules/{dry_planned['id']}/apply-planned").json()
    assert resp == {"applied": 2, "failed": 2, "emails": 2}

    from app.models import EmailAction
    db_session.expire_all()
    by_email = {}
    for a in db_session.query(EmailAction).all():
        by_email.setdefault(a.email_id, []).append(a)
    emails = {e.gmail_message_id: e.id for e in db_session.query(Email).all()}
    assert all(a.error for a in by_email[emails["m1"]])       # failed, kept pending
    assert all(not a.executed for a in by_email[emails["m1"]])
    assert all(a.executed for a in by_email[emails["m2"]])    # succeeded
