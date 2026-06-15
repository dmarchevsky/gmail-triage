"""Labels as first-class entities: CRUD, Gmail color sync, rule resolution."""

import json

import pytest
import respx

from app.services.gmail import GMAIL_API
from tests.test_m1_gmail import CLIENT_SECRET_JSON, b64url, gmail_message, make_token
from tests.test_m2_classification import CHAT_URL, llm_response

GREEN = {"text_color": "#ffffff", "background_color": "#16a766"}


@pytest.fixture()
def connected(client, db_session):
    from app.services import gmail, settings_service

    settings_service.set_setting(db_session, "gmail_client_secret_json", CLIENT_SECRET_JSON)
    gmail.save_token(db_session, make_token(), email="me@gmail.test")
    db_session.commit()


def test_palette_endpoint(auth_client):
    palette = auth_client.get("/api/v1/labels/palette").json()
    assert isinstance(palette, list) and palette
    assert all({"background", "text"} <= p.keys() for p in palette)


def test_create_label_validates_color(auth_client):
    bad = auth_client.post("/api/v1/labels", json={
        "name": "X", "text_color": "#123456", "background_color": "#abcdef"})
    assert bad.status_code == 400
    ok = auth_client.post("/api/v1/labels", json={"name": "X", **GREEN})
    assert ok.status_code == 201
    assert ok.json()["background_color"] == "#16a766"
    # duplicate
    assert auth_client.post("/api/v1/labels", json={"name": "X", **GREEN}).status_code == 409


def test_create_label_no_color_allowed(auth_client):
    assert auth_client.post("/api/v1/labels", json={"name": "Plain"}).status_code == 201


@respx.mock
def test_create_label_syncs_color_to_gmail(auth_client, db_session, connected):
    respx.get(f"{GMAIL_API}/labels").respond(200, json={"labels": []})
    create = respx.post(f"{GMAIL_API}/labels").respond(
        200, json={"id": "Label_9", "name": "News"})
    resp = auth_client.post("/api/v1/labels", json={"name": "News", **GREEN})
    assert resp.status_code == 201

    sent = json.loads(create.calls[0].request.content)
    assert sent["color"] == {"textColor": "#ffffff", "backgroundColor": "#16a766"}
    from app.models import Label
    db_session.expire_all()
    assert db_session.query(Label).filter_by(name="News").one().gmail_label_id == "Label_9"


def test_delete_label_blocked_while_used_by_rule(auth_client, db_session):
    label = auth_client.post("/api/v1/labels", json={"name": "Keep", **GREEN}).json()
    auth_client.post("/api/v1/rules", json={
        "name": "uses label", "actions": [{"type": "add_label", "label_id": label["id"]}]})

    blocked = auth_client.delete(f"/api/v1/labels/{label['id']}")
    assert blocked.status_code == 409
    assert "uses label" in blocked.json()["detail"]

    forced = auth_client.delete(f"/api/v1/labels/{label['id']}?force=true")
    assert forced.status_code == 200
    user_labels = [lb for lb in auth_client.get("/api/v1/labels").json() if not lb["is_system"]]
    assert user_labels == []


def test_rule_rejects_unknown_label_id(auth_client):
    resp = auth_client.post("/api/v1/rules", json={
        "name": "bad", "actions": [{"type": "add_label", "label_id": 999}]})
    assert resp.status_code == 400


def test_quick_label_creates_label_and_dry_run_rule(auth_client, db_session):
    cat = auth_client.post("/api/v1/categories", json={
        "name": "News", "criteria_md": "n"}).json()
    resp = auth_client.post(f"/api/v1/categories/{cat['id']}/quick-label", json={
        "name": "MailTriage/News", "min_confidence": 0.7, **GREEN})
    assert resp.status_code == 201

    from app.models import Label, Rule
    label = db_session.query(Label).filter_by(name="MailTriage/News").one()
    rule = db_session.query(Rule).filter_by(match_category_id=cat["id"]).one()
    assert rule.dry_run is True
    assert rule.actions == [{"type": "add_label", "label_id": label.id}]
    assert rule.match_min_confidence == 0.7
    assert label.background_color == "#16a766"


@respx.mock
def test_rule_applies_colored_label_on_first_use(auth_client, db_session, connected):
    """A live add_label rule creates the Gmail label WITH the stored color and
    applies it."""
    from app.models import Email, Label

    cat = auth_client.post("/api/v1/categories", json={
        "name": "MarketNews", "criteria_md": "m"}).json()
    label = Label(name="MailTriage/MarketNews", text_color="#ffffff",
                  background_color="#16a766")
    db_session.add(label)
    db_session.add(Email(gmail_message_id="m1", sender="Brew <crew@brew.com>",
                         sender_domain="brew.com", subject="s", snippet="x",
                         status="pending"))
    db_session.commit()
    auth_client.post("/api/v1/rules", json={
        "name": "label it", "match_category_id": cat["id"], "dry_run": False,
        "actions": [{"type": "add_label", "label_id": label.id}]})

    full = gmail_message("m1")
    full["payload"]["parts"] = [{"mimeType": "text/plain",
                                 "body": {"data": b64url("Body.")}}]
    respx.get(f"{GMAIL_API}/messages/m1").respond(200, json=full)
    respx.post(CHAT_URL).mock(return_value=llm_response(
        {"category": "MarketNews", "confidence": 0.9, "rationale": "r"}))
    respx.get(f"{GMAIL_API}/labels").respond(200, json={"labels": []})
    create = respx.post(f"{GMAIL_API}/labels").respond(
        200, json={"id": "Label_5", "name": "MailTriage/MarketNews"})
    modify = respx.post(f"{GMAIL_API}/messages/m1/modify").respond(200, json={})

    auth_client.post("/api/v1/classify/run-now")

    sent = json.loads(create.calls[0].request.content)
    assert sent["color"] == {"textColor": "#ffffff", "backgroundColor": "#16a766"}
    assert json.loads(modify.calls[0].request.content)["addLabelIds"] == ["Label_5"]
    db_session.expire_all()
    assert db_session.query(Label).filter_by(name="MailTriage/MarketNews").one() \
        .gmail_label_id == "Label_5"
