"""Measure the decider against known-good labels before changing prod.

Run inside the app container (read-only apart from a possible Gmail token
refresh):

    python -m app.tools.eval_decider --limit 150
    python -m app.tools.eval_decider --overrides - < proposed.json

Ground truth per email: the latest feedback correction if any, else the stored
category from the generative LLM. Emails classified by the decider itself,
hard rules or pins are only used when they carry a feedback correction.

`--overrides` JSON: {"criteria": {"<category>": "<criteria_md>"},
"pins": {"<category>": ["<pattern>", ...]}}. Overridden criteria replace the
stored text for this run only; pins replace all stored pins, and pinned
emails are scored against truth separately instead of going to the decider.
"""

import argparse
import asyncio
import json
import sys
from collections import Counter
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_sessionmaker
from app.models import Category, Email, EmailStatus, Feedback
from app.services import decider, gmail, llm, settings_service
from app.services.gmail import GmailClient, GmailNotFound
from app.services.matchers import sender_matches

NONE = decider.NONE_OPTION
THRESHOLDS = [0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]


def score(records: list[dict]) -> dict:
    """Agreement, confusions and threshold tables for decider records with
    keys truth, pred, confidence, probability."""
    n = len(records)
    correct = sum(r["truth"] == r["pred"] for r in records)
    per_truth: dict[str, Counter] = {}
    for r in records:
        per_truth.setdefault(r["truth"], Counter())[r["pred"]] += 1
    categories = sorted({r["truth"] for r in records} | {r["pred"] for r in records})
    per_category = {}
    for name in categories:
        actual = sum(1 for r in records if r["truth"] == name)
        predicted = sum(1 for r in records if r["pred"] == name)
        hits = sum(1 for r in records if r["truth"] == name and r["pred"] == name)
        per_category[name] = {
            "truth": actual, "predicted": predicted, "correct": hits,
            "recall": round(hits / actual, 3) if actual else None,
            "precision": round(hits / predicted, 3) if predicted else None,
        }
    confusions = Counter((r["truth"], r["pred"]) for r in records if r["truth"] != r["pred"])
    thresholds: dict[str, list[dict]] = {}
    for metric in ("confidence", "probability"):
        rows = []
        for t in THRESHOLDS:
            kept = [r for r in records if r[metric] >= t]
            rows.append({
                "threshold": t,
                "coverage": round(len(kept) / n, 3) if n else None,
                "precision": round(sum(r["truth"] == r["pred"] for r in kept) / len(kept), 3)
                if kept else None,
            })
        thresholds[metric] = rows
    return {
        "n": n,
        "agreement": round(correct / n, 3) if n else None,
        "none_rate": round(sum(r["pred"] == NONE for r in records) / n, 3) if n else None,
        "per_category": per_category,
        "confusions": [{"truth": t, "pred": p, "n": c} for (t, p), c in confusions.most_common(15)],
        "thresholds": thresholds,
    }


def score_pins(pinned: list[dict]) -> dict:
    wrong = [r for r in pinned if r["truth"] != r["pred"]]
    return {"n": len(pinned),
            "accuracy": round(1 - len(wrong) / len(pinned), 3) if pinned else None,
            "mismatches": wrong[:20]}


def _truths(session: Session) -> dict[int, str]:
    """Latest feedback correction per email, as a category name (or none)."""
    names = {c.id: c.name for c in session.scalars(select(Category))}
    out: dict[int, str] = {}
    for fb in session.scalars(select(Feedback).order_by(Feedback.created_at)):
        out[fb.email_id] = names.get(fb.correct_category_id, NONE) \
            if fb.correct_category_id else NONE
    return out


def sample(session: Session, limit: int, decider_model: str) -> list[tuple[Email, str]]:
    corrected = _truths(session)
    names = {c.id: c.name for c in session.scalars(select(Category))}
    rows = session.scalars(
        select(Email)
        .where(Email.status.in_([EmailStatus.classified.value, EmailStatus.actioned.value]))
        .order_by(Email.classified_at.desc())
        .limit(limit * 3))
    out: list[tuple[Email, str]] = []
    for email in rows:
        if email.id in corrected:
            out.append((email, corrected[email.id]))
        elif email.llm_model and email.llm_model != decider_model:
            out.append((email, names.get(email.classification_id, NONE)
                        if email.classification_id else NONE))
        if len(out) >= limit:
            break
    return out


def _categories(session: Session, overrides: dict) -> list[Category]:
    """Detached copies of the enabled categories with overrides applied."""
    stored = list(session.scalars(
        select(Category).where(Category.enabled.is_(True)).order_by(Category.id)))
    by_name = {c.name: c for c in stored}
    for key in ("criteria", "pins"):
        unknown = set(overrides.get(key) or {}) - set(by_name)
        if unknown:
            sys.exit(f"eval_decider: unknown categories in {key}: {sorted(unknown)}")
    pins = overrides.get("pins")
    return [Category(id=c.id, name=c.name,
                     criteria_md=(overrides.get("criteria") or {}).get(c.name, c.criteria_md),
                     sender_patterns=(pins.get(c.name, []) if pins is not None
                                      else c.sender_patterns or []))
            for c in stored]


async def run(limit: int, overrides: dict) -> dict:
    session = get_sessionmaker()()
    try:
        settings = settings_service.get_all_settings(session, redact=False)
        categories = _categories(session, overrides)
        _, model = decider.resolve_decider_target(settings)
        emails = sample(session, limit, model)
        client = GmailClient(session, settings["gmail_client_secret_json"])
        records: list[dict] = []
        pinned: list[dict] = []
        errors: list[str] = []
        try:
            for i, (email, truth) in enumerate(emails, 1):
                print(f"\r{i}/{len(emails)}", end="", file=sys.stderr, flush=True)
                pin = next((c for c in categories for p in c.sender_patterns or []
                            if sender_matches([p], email.sender or "")), None)
                base = {"email_id": email.id, "sender": email.sender,
                        "subject": (email.subject or "")[:70], "truth": truth}
                if pin is not None:
                    pinned.append({**base, "pred": pin.name})
                    continue
                try:
                    body = email.body_text
                    if body is None:
                        msg = await client.get_message_full(email.gmail_message_id)
                        body = gmail.extract_body_text(msg.get("payload", {}))
                    result = await decider.decide_category(categories, email, body, settings)
                except (GmailNotFound, llm.LLMError) as e:
                    errors.append(f"email {email.id}: {e}"[:200])
                    continue
                records.append({**base, "pred": result.choice,
                                "confidence": result.confidence,
                                "probability": result.probabilities.get(result.choice, 0.0)})
        finally:
            await client.aclose()
        print(file=sys.stderr)
        return {"model": model, "decider": score(records), "pins": score_pins(pinned),
                "errors": errors, "records": records}
    finally:
        session.rollback()
        session.close()


def print_report(report: dict[str, Any]) -> None:
    d, p = report["decider"], report["pins"]
    print(f"decider {report['model']}: n={d['n']} agreement={d['agreement']} "
          f"none_rate={d['none_rate']}")
    if p["n"]:
        print(f"pinned: n={p['n']} accuracy={p['accuracy']}")
        for m in p["mismatches"]:
            print(f"  pin→{m['pred']} but truth {m['truth']}: {m['sender']} | {m['subject']}")
    print("\nper category        truth  pred  ok  recall  precision")
    for name, s in d["per_category"].items():
        print(f"  {name[:18]:18} {s['truth']:5} {s['predicted']:5} {s['correct']:3}  "
              f"{s['recall'] if s['recall'] is not None else '-':>6}  "
              f"{s['precision'] if s['precision'] is not None else '-':>6}")
    print("\ntop confusions (truth → decider)")
    for c in d["confusions"]:
        print(f"  {c['n']:3}  {c['truth']} → {c['pred']}")
    for metric, rows in d["thresholds"].items():
        print(f"\nthreshold on {metric}: coverage / precision")
        for r in rows:
            print(f"  ≥{r['threshold']:.1f}  {r['coverage']}  {r['precision']}")
    if report["errors"]:
        print(f"\n{len(report['errors'])} errors, first: {report['errors'][0]}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate the decider on recent mail.")
    parser.add_argument("--limit", type=int, default=150)
    parser.add_argument("--overrides", help="JSON file with criteria/pins overrides, or -")
    parser.add_argument("--json", action="store_true", help="print the full JSON report")
    args = parser.parse_args()
    overrides: dict = {}
    if args.overrides == "-":
        overrides = json.load(sys.stdin)
    elif args.overrides:
        with open(args.overrides) as f:
            overrides = json.load(f)
    report = asyncio.run(run(args.limit, overrides))
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print_report(report)


if __name__ == "__main__":
    main()
