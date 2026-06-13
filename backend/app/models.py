"""SQLAlchemy models per spec §3."""

from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator):
    """UTC-normalized datetime column.

    SQLite has no tz-aware storage: values written aware are normalized to
    UTC, and values read back (naive) are tagged as UTC so `.isoformat()`
    carries the +00:00 offset and Python arithmetic is tz-correct.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None and value.tzinfo is not None:
            value = value.astimezone(UTC)
        return value

    def process_result_value(self, value, dialect):
        if value is None:
            return value
        if value.tzinfo is None:  # SQLite returns naive (stored UTC)
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)  # Postgres returns aware in session tz


class Base(DeclarativeBase):
    pass


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[dict | list | str | int | float | bool | None] = mapped_column(JSON)


class GmailAuth(Base):
    __tablename__ = "gmail_auth"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_json: Mapped[str] = mapped_column(Text)  # Fernet-encrypted
    email_address: Mapped[str | None] = mapped_column(String(320))
    granted_scopes: Mapped[list | None] = mapped_column(JSON)
    history_id: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow,
                                                 onupdate=utcnow)


class Label(Base):
    """A Gmail label, applied to emails by rules. Separate from categories."""

    __tablename__ = "labels"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    gmail_label_id: Mapped[str | None] = mapped_column(String(64))
    text_color: Mapped[str | None] = mapped_column(String(16))
    background_color: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow,
                                                 onupdate=utcnow)


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    criteria_md: Mapped[str] = mapped_column(Text, default="")
    criteria_version: Mapped[int] = mapped_column(Integer, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow,
                                                 onupdate=utcnow)

    criteria_history: Mapped[list["CategoryCriteriaHistory"]] = relationship(
        back_populates="category", cascade="all, delete-orphan"
    )


class CriteriaSource(StrEnum):
    user = "user"
    llm_feedback = "llm_feedback"


class CategoryCriteriaHistory(Base):
    __tablename__ = "category_criteria_history"
    __table_args__ = (UniqueConstraint("category_id", "version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id", ondelete="CASCADE"))
    version: Mapped[int] = mapped_column(Integer)
    criteria_md: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(16), default=CriteriaSource.user.value)
    feedback_ids: Mapped[list | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    category: Mapped[Category] = relationship(back_populates="criteria_history")


class Rule(Base):
    __tablename__ = "rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int] = mapped_column(Integer, default=100)  # lower first
    match_category_id: Mapped[int | None] = mapped_column(
        ForeignKey("categories.id", ondelete="SET NULL"), nullable=True
    )
    match_min_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    match_sender_pattern: Mapped[str | None] = mapped_column(Text)
    actions: Mapped[list] = mapped_column(JSON, default=list)  # see §4.3
    stop_processing: Mapped[bool] = mapped_column(Boolean, default=True)
    # Per-rule dry-run: True records planned actions without executing.
    dry_run: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow,
                                                 onupdate=utcnow)


class EmailStatus(StrEnum):
    pending = "pending"
    classified = "classified"
    actioned = "actioned"
    skipped = "skipped"
    error = "error"


class Email(Base):
    __tablename__ = "emails"

    id: Mapped[int] = mapped_column(primary_key=True)
    gmail_message_id: Mapped[str] = mapped_column(String(32), unique=True)
    gmail_thread_id: Mapped[str | None] = mapped_column(String(32))
    history_id: Mapped[str | None] = mapped_column(String(64))
    received_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    sender: Mapped[str | None] = mapped_column(String(512))
    sender_domain: Mapped[str | None] = mapped_column(String(255), index=True)
    subject: Mapped[str | None] = mapped_column(Text)
    snippet: Mapped[str | None] = mapped_column(Text)
    body_text: Mapped[str | None] = mapped_column(Text)  # only when store_bodies=true
    body_text_hash: Mapped[str | None] = mapped_column(String(64))
    size_estimate: Mapped[int | None] = mapped_column(Integer)
    classification_id: Mapped[int | None] = mapped_column(
        ForeignKey("categories.id", ondelete="SET NULL"), nullable=True
    )
    confidence: Mapped[float | None] = mapped_column(Float)
    rationale: Mapped[str | None] = mapped_column(Text)
    llm_model: Mapped[str | None] = mapped_column(String(128))
    classified_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    status: Mapped[str] = mapped_column(String(16), default=EmailStatus.pending.value, index=True)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    classification: Mapped[Category | None] = relationship()
    actions: Mapped[list["EmailAction"]] = relationship(
        back_populates="email", cascade="all, delete-orphan"
    )


class EmailAction(Base):
    __tablename__ = "email_actions"

    id: Mapped[int] = mapped_column(primary_key=True)
    email_id: Mapped[int] = mapped_column(ForeignKey("emails.id", ondelete="CASCADE"))
    rule_id: Mapped[int | None] = mapped_column(ForeignKey("rules.id", ondelete="SET NULL"))
    action_type: Mapped[str] = mapped_column(String(32))
    action_params: Mapped[dict | None] = mapped_column(JSON)
    executed: Mapped[bool] = mapped_column(Boolean, default=False)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=True)
    executed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    error: Mapped[str | None] = mapped_column(Text)

    email: Mapped[Email] = relationship(back_populates="actions")


class FeedbackStatus(StrEnum):
    open = "open"
    incorporated = "incorporated"
    dismissed = "dismissed"


class ProposalStatus(StrEnum):
    none = "none"
    pending_review = "pending_review"
    approved = "approved"
    rejected = "rejected"


class Feedback(Base):
    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(primary_key=True)
    email_id: Mapped[int] = mapped_column(ForeignKey("emails.id", ondelete="CASCADE"))
    correct_category_id: Mapped[int | None] = mapped_column(
        ForeignKey("categories.id", ondelete="SET NULL"), nullable=True
    )
    user_note: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default=FeedbackStatus.open.value)
    proposed_criteria_md: Mapped[str | None] = mapped_column(Text)
    proposal_explanation: Mapped[str | None] = mapped_column(Text)
    proposal_status: Mapped[str] = mapped_column(String(16), default=ProposalStatus.none.value)
    # On the representative row of a consolidated proposal: the feedback ids
    # (incl. itself) the proposal covers, so all are considered + incorporated.
    proposal_feedback_ids: Mapped[list | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    email: Mapped[Email] = relationship()


class Digest(Base):
    __tablename__ = "digests"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    category_ids: Mapped[list] = mapped_column(JSON, default=list)
    cron_times: Mapped[list] = mapped_column(JSON, default=list)  # ["07:00","16:00"]
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    min_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    prompt_template: Mapped[str | None] = mapped_column(Text)
    telegram_chat_id: Mapped[str | None] = mapped_column(String(64))
    include_links: Mapped[bool] = mapped_column(Boolean, default=True)
    include_metadata: Mapped[bool] = mapped_column(Boolean, default=True)
    max_emails: Mapped[int] = mapped_column(Integer, default=50)
    send_no_news: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow,
                                                 onupdate=utcnow)


class DigestRunStatus(StrEnum):
    running = "running"
    success = "success"
    error = "error"
    dry_run = "dry_run"
    empty = "empty"


class DigestRun(Base):
    __tablename__ = "digest_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    digest_id: Mapped[int] = mapped_column(ForeignKey("digests.id", ondelete="CASCADE"))
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    status: Mapped[str] = mapped_column(String(16), default=DigestRunStatus.running.value)
    email_ids: Mapped[list] = mapped_column(JSON, default=list)
    summary_text: Mapped[str | None] = mapped_column(Text)
    telegram_message_id: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(16))  # system|user|scheduler
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict | None] = mapped_column(JSON)
