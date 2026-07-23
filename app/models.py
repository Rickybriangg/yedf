"""Domain model.

Standalone minimal scaffold of the RecoverIQ domain plus the automation-layer
tables. Only what Milestone 1 needs is exercised by logic here; the automation
tables (rules/events/templates) are the core of this milestone, and a minimal
Loan / RecoveryCase are scaffolded so the engine has something to run against.

Enums are stored as plain strings (portable across SQLite/Postgres and easy to
inspect in the DB) with the allowed values pinned as module constants.
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

# --- Enumerations (kept as string constants, validated at the edges) ---------

# AutomationRule.action
ACTIONS = (
    "SendReminder",
    "SendPaymentLink",
    "GenerateLetter",
    "Escalate",
    "FlagForCRB",
    "Notify",
)

# AutomationRule.channel
CHANNELS = ("SMS", "USSD", "Email", "Letter", "InApp")

# Channels that actually contact a borrower — these must respect doNotContact.
CONTACT_CHANNELS = frozenset({"SMS", "USSD", "Email", "Letter"})

# AutomationEvent.status
EVENT_STATUSES = ("Scheduled", "Sent", "Failed", "Skipped", "Cancelled")


class Loan(Base):
    """Minimal standalone Loan. In the full RecoverIQ system this would already
    exist; here we scaffold just the fields the automation engine reads."""

    __tablename__ = "loans"

    loan_no: Mapped[str] = mapped_column(String, primary_key=True)
    borrower_name: Mapped[str] = mapped_column(String, nullable=False)
    borrower_phone: Mapped[str | None] = mapped_column(String, nullable=True)
    product: Mapped[str] = mapped_column(String, nullable=False, default="Personal")

    outstanding_balance: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    disbursed_date: Mapped[date] = mapped_column(Date, nullable=False)
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    last_payment_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    last_action_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    # Derived fields, refreshed by the nightly re-tiering job (see tiering.py).
    recovery_tier: Mapped[str | None] = mapped_column(String, nullable=True)
    arrears_bucket: Mapped[str | None] = mapped_column(String, nullable=True)
    dormancy_days: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Guardrail flags — checked as late as possible before any send.
    automation_paused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    do_not_contact: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    cases: Mapped[list["RecoveryCase"]] = relationship(
        back_populates="loan", cascade="all, delete-orphan"
    )


class RecoveryCase(Base):
    """Minimal recovery case. The escalation state machine (Milestone 3) will
    drive currentStage/enteredStageAt; scaffolded now so the schema is stable."""

    __tablename__ = "recovery_cases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    loan_no: Mapped[str] = mapped_column(ForeignKey("loans.loan_no"), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="Open")
    current_stage: Mapped[str | None] = mapped_column(String, nullable=True)
    entered_stage_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    loan: Mapped["Loan"] = relationship(back_populates="cases")


class MessageTemplate(Base):
    """Merge-field template for reminders/letters. Rendering arrives in
    Milestone 2/4; the table is defined now so rules can reference it."""

    __tablename__ = "message_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    channel: Mapped[str] = mapped_column(String, nullable=False)
    subject: Mapped[str | None] = mapped_column(String, nullable=True)
    body: Mapped[str] = mapped_column(String, nullable=False)
    language: Mapped[str] = mapped_column(String, nullable=False, default="en")


class AutomationRule(Base):
    """A rule is configurable *data*, not hardcoded logic. condition is a
    structured JSON matcher (see rule_engine.py) — never eval()'d."""

    __tablename__ = "automation_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # e.g. {"tier": "Curable", "daysPastDue": {"gte": 1, "lte": 3}}
    condition: Mapped[dict] = mapped_column(JSON, nullable=False)

    action: Mapped[str] = mapped_column(String, nullable=False)
    channel: Mapped[str] = mapped_column(String, nullable=False)
    template_id: Mapped[int | None] = mapped_column(
        ForeignKey("message_templates.id"), nullable=True
    )
    cooldown_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # If true, the engine creates a pending (Scheduled) recommendation rather
    # than firing directly — nothing that removes discretion runs unattended.
    requires_approval: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class AutomationEvent(Base):
    """Append-only audit row for every automated action. Every event is
    traceable to the rule that caused it and carries the reason it fired."""

    __tablename__ = "automation_events"
    __table_args__ = (
        # Idempotency backstop: a given rule can produce at most one event per
        # loan per trigger period (the run date). The engine also checks this in
        # code, but the constraint guarantees re-runs can never double-insert.
        UniqueConstraint("trigger_key", name="uq_automation_event_trigger_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    loan_no: Mapped[str] = mapped_column(ForeignKey("loans.loan_no"), nullable=False)
    rule_id: Mapped[int] = mapped_column(ForeignKey("automation_rules.id"), nullable=False)
    triggered_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    action: Mapped[str] = mapped_column(String, nullable=False)
    channel: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String, nullable=False)
    reason: Mapped[str] = mapped_column(String, nullable=False, default="")

    # Deduplication key = f"{rule_id}:{loan_no}:{run_date}". Unique.
    trigger_key: Mapped[str] = mapped_column(String, nullable=False)
