"""eval_decider: scoring maths, ground-truth sampling, overrides and pins."""

import json

import respx
from httpx import Response

from tests import test_m2_classification as m2

connected = m2.connected

SYSTEMONE_URL = f"{m2.LLM_BASE}/systemone"


def test_score_agreement_confusions_and_thresholds():
    from app.tools.eval_decider import score

    records = [
        {"truth": "Ads", "pred": "Ads", "confidence": 0.9, "probability": 0.95},
        {"truth": "Ads", "pred": "Not Important", "confidence": 0.4, "probability": 0.5},
        {"truth": "Orders", "pred": "Orders", "confidence": 0.6, "probability": 0.7},
        {"truth": "Orders", "pred": "none", "confidence": 0.3, "probability": 0.35},
    ]
    report = score(records)
    assert report["n"] == 4
    assert report["agreement"] == 0.5
    assert report["none_rate"] == 0.25
    assert report["per_category"]["Ads"] == {"truth": 2, "predicted": 1, "correct": 1,
                                             "recall": 0.5, "precision": 1.0}
    assert {"truth": "Ads", "pred": "Not Important", "n": 1} in report["confusions"]
    at_05 = next(r for r in report["thresholds"]["confidence"] if r["threshold"] == 0.5)
    assert at_05 == {"threshold": 0.5, "coverage": 0.5, "precision": 1.0}


def test_score_empty():
    from app.tools.eval_decider import score
    assert score([])["agreement"] is None


def _seed(auth_client, db_session):
    from app.models import Category, Email, Feedback

    for name in ("Ads", "Orders"):
        assert auth_client.post("/api/v1/categories",
                                json={"name": name, "criteria_md": f"{name} stuff"}
                                ).status_code == 201
    ads = db_session.query(Category).filter_by(name="Ads").one()
    orders = db_session.query(Category).filter_by(name="Orders").one()
    common = {"status": "classified", "body_text": "body"}
    rows = [
        Email(gmail_message_id="a", sender="deals@shop.com", subject="Sale",
              classification_id=ads.id, llm_model="gemma", **common),
        Email(gmail_message_id="b", sender="orders@shop.com", subject="Shipped",
              classification_id=ads.id, llm_model="gemma", **common),  # corrected below
        Email(gmail_message_id="c", sender="x@decided.com", subject="By decider",
              classification_id=ads.id, llm_model="d3-nano", **common),  # no truth
        Email(gmail_message_id="d", sender="bot@usps.com", subject="Mail",
              classification_id=orders.id, llm_model="gemma", **common),
    ]
    db_session.add_all(rows)
    db_session.flush()
    db_session.add(Feedback(email_id=rows[1].id, correct_category_id=orders.id))
    db_session.commit()


@respx.mock
async def test_run_uses_truth_overrides_and_pins(auth_client, db_session, connected):
    from app.tools.eval_decider import run

    _seed(auth_client, db_session)
    answers = {"Sale": "Ads", "Shipped": "Ads"}

    def respond(request):
        body = json.loads(request.content)
        assert body["questions"]["category"]["criteria"]["Ads"] == "ADS OVERRIDE"
        subject = next(v for k, v in answers.items() if f"Subject: {k}" in body["state"])
        return Response(200, json={"model": "d3-nano", "answers": {"category": {
            "choice": subject, "probabilities": {subject: 0.8}, "confidence": 0.7}}})

    respx.post(SYSTEMONE_URL).mock(side_effect=respond)
    report = await run(10, {"criteria": {"Ads": "ADS OVERRIDE"},
                            "pins": {"Orders": ["usps.com"]}})

    d = report["decider"]
    assert d["n"] == 2                       # decider-classified email excluded
    assert d["agreement"] == 0.5             # "Shipped" truth comes from feedback
    assert {"truth": "Orders", "pred": "Ads", "n": 1} in d["confusions"]
    assert report["pins"] == {"n": 1, "accuracy": 1.0, "mismatches": []}

    # nothing persisted: stored criteria untouched
    from app.models import Category
    db_session.expire_all()
    assert db_session.query(Category).filter_by(name="Ads").one().criteria_md == "Ads stuff"


async def test_run_rejects_unknown_override_category(auth_client, db_session, connected):
    import pytest

    from app.tools.eval_decider import run

    _seed(auth_client, db_session)
    with pytest.raises(SystemExit, match="Nope"):
        await run(5, {"criteria": {"Nope": "x"}})
