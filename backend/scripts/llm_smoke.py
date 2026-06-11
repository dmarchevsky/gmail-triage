#!/usr/bin/env python3
"""Live smoke test against a real llama.cpp endpoint (M2 acceptance).

Usage: LLM_BASE_URL=http://localhost:8081/v1 python scripts/llm_smoke.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import llm  # noqa: E402

SCHEMA = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "enum": ["MarketNews", "Receipts", "none"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "rationale": {"type": "string"},
    },
    "required": ["category", "confidence", "rationale"],
    "additionalProperties": False,
}

SYSTEM = llm.load_prompt("classification_system.txt")
USER = llm.load_prompt("classification_user.txt").format(
    categories_block=(
        "### MarketNews\nDaily/weekly market commentary, stock and macro analysis "
        "newsletters.\n\n### Receipts\nOrder confirmations, invoices, payment receipts."
    ),
    sender="Morning Brew <crew@morningbrew.com>",
    subject="Stocks slide as yields spike",
    date="2026-06-11T07:00:00+00:00",
    body="Futures fell this morning as the 10-year Treasury yield jumped above 5%. "
         "Tech led the decline; the VIX rose 12%...",
)


async def main() -> int:
    probe = await llm.health_probe()
    print("health:", probe)
    if not probe["ok"]:
        return 1
    result = await llm.chat_json(SYSTEM, USER, SCHEMA, "email_classification",
                                 timeout=120)
    print("classification:", result)
    assert result["category"] == "MarketNews", "expected MarketNews"
    assert 0 <= result["confidence"] <= 1
    print("SMOKE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
