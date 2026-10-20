"""Sender pins: `Category.sender_patterns` that classify without the model.

Every pattern points to exactly one category, compared case-insensitively
(matching itself is case-insensitive, see services/matchers.py).
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Category


def normalize_patterns(patterns: list[str]) -> list[str]:
    """Trimmed, non-empty, de-duplicated (case-insensitive), order kept."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in patterns:
        pattern = (raw or "").strip()
        if pattern and pattern.lower() not in seen:
            seen.add(pattern.lower())
            out.append(pattern)
    return out


def find_conflict(session: Session, patterns: list[str],
                  exclude_category_id: int | None = None) -> tuple[str, Category] | None:
    """The first pattern already pinned to another category, with that category."""
    wanted = {p.lower(): p for p in patterns}
    for other in session.scalars(select(Category).order_by(Category.id)):
        if other.id == exclude_category_id:
            continue
        for existing in other.sender_patterns or []:
            if existing.lower() in wanted:
                return wanted[existing.lower()], other
    return None


def pin_sender(session: Session, category: Category, pattern: str) -> None:
    """Pin `pattern` to `category`, moving it off any other category."""
    key = pattern.lower()
    for other in session.scalars(select(Category).where(Category.id != category.id)):
        kept = [p for p in other.sender_patterns or [] if p.lower() != key]
        if len(kept) != len(other.sender_patterns or []):
            other.sender_patterns = kept
    category.sender_patterns = normalize_patterns([*(category.sender_patterns or []), pattern])
