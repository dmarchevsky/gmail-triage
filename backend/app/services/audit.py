"""Audit log helper."""

from typing import Any

from sqlalchemy.orm import Session

from app.models import AuditLog


def audit(session: Session, actor: str, event_type: str,
          payload: dict[str, Any] | None = None) -> None:
    session.add(AuditLog(actor=actor, event_type=event_type, payload=payload or {}))
