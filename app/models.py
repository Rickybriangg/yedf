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
    """A recovery case, driven by the escalation state machine (Milestone 3).

    A case walks the EscalationPath configured for its tier. current_stage_index
    points at the active stage; entered_stage_at is when it landed there;
    path_entered_at is when it joined the path (stage 0). A logged payment or
    response stops advancement and flags the case for a human to close."""

    __tablename__ = "recovery_cases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    loan_no: Mapped[str] = mapped_column(ForeignKey("loans.loan_no"), nullable=False)
    # Open | ReviewToClose | Closed
    status: Mapped[str] = mapped_column(String, nullable=False, default="Open")

    tier: Mapped[str | None] = mapped_column(String, nullable=True)
    current_stage: Mapped[str | None] = mapped_column(String, nullable=True)
    current_stage_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    entered_stage_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    path_entered_at: Mapped[date | None] = mapped_column(Date, nullable=True)

    flagged_for_review: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    review_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    closed_at: Mapped[date | None] = mapped_column(Date, nullable=True)

    loan: Mapped["Loan"] = relationship(back_populates="cases")
    actions: Mapped[list["RecoveryAction"]] = relationship(
        back_populates="case", cascade="all, delete-orphan"
    )


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
    # Nullable: escalation / batch / payment / CRB events are not tied to a rule.
    rule_id: Mapped[int | None] = mapped_column(
        ForeignKey("automation_rules.id"), nullable=True
    )
    # What produced this event: rule | escalation | batch | payment | crb.
    source: Mapped[str] = mapped_column(String, nullable=False, default="rule")
    triggered_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    action: Mapped[str] = mapped_column(String, nullable=False)
    channel: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String, nullable=False)
    reason: Mapped[str] = mapped_column(String, nullable=False, default="")

    # Deduplication key = f"{rule_id}:{loan_no}:{run_date}". Unique.
    trigger_key: Mapped[str] = mapped_column(String, nullable=False)


# ---------------------------------------------------------------------------
# Milestones 2–6 tables
# ---------------------------------------------------------------------------

# RecoveryAction.type
ACTION_PAYMENT = "Payment received"
RESPONSE_ACTION_TYPES = frozenset({ACTION_PAYMENT, "Borrower responded", "Promise to pay"})


class RecoveryAction(Base):
    """A logged recovery action or borrower response on a case. Used by the
    escalation engine to detect that a case should stop advancing."""

    __tablename__ = "recovery_actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    loan_no: Mapped[str] = mapped_column(ForeignKey("loans.loan_no"), nullable=False)
    case_id: Mapped[int | None] = mapped_column(ForeignKey("recovery_cases.id"), nullable=True)
    type: Mapped[str] = mapped_column(String, nullable=False)
    note: Mapped[str] = mapped_column(String, nullable=False, default="")
    created_at: Mapped[date] = mapped_column(Date, nullable=False)

    case: Mapped["RecoveryCase | None"] = relationship(back_populates="actions")


class OutboundMessage(Base):
    """What the (stubbed) gateway 'sent'. ConsoleSmsGateway writes here instead
    of contacting a real provider — the rendered text is the audit record."""

    __tablename__ = "outbound_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    loan_no: Mapped[str] = mapped_column(ForeignKey("loans.loan_no"), nullable=False)
    event_id: Mapped[int | None] = mapped_column(ForeignKey("automation_events.id"), nullable=True)
    channel: Mapped[str] = mapped_column(String, nullable=False)
    to_addr: Mapped[str | None] = mapped_column(String, nullable=True)
    subject: Mapped[str | None] = mapped_column(String, nullable=True)
    body: Mapped[str] = mapped_column(String, nullable=False)
    template_id: Mapped[int | None] = mapped_column(
        ForeignKey("message_templates.id"), nullable=True
    )
    provider: Mapped[str] = mapped_column(String, nullable=False, default="ConsoleSmsGateway")
    status: Mapped[str] = mapped_column(String, nullable=False, default="Sent")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


# PaymentLink.provider / status
PAYMENT_PROVIDERS = ("MpesaSTK", "Manual")
PAYMENT_STATUSES = ("Pending", "Paid", "Expired", "Failed")


class PaymentLink(Base):
    """A payment request. In dev the STK push and callback are stubbed; the
    `token` is what a callback references to mark this link Paid."""

    __tablename__ = "payment_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    loan_no: Mapped[str] = mapped_column(ForeignKey("loans.loan_no"), nullable=False)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    provider: Mapped[str] = mapped_column(String, nullable=False, default="MpesaSTK")
    status: Mapped[str] = mapped_column(String, nullable=False, default="Pending")
    token: Mapped[str] = mapped_column(String, nullable=False)
    external_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class EscalationPath(Base):
    """Ordered escalation stages for a tier, stored as data so a manager can
    tune day-offsets without a code change. stages is a JSON list of
    {stage, action, channel, offsetDays} dicts, ascending by offsetDays."""

    __tablename__ = "escalation_paths"
    __table_args__ = (UniqueConstraint("tier", name="uq_escalation_path_tier"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tier: Mapped[str] = mapped_column(String, nullable=False)
    stages: Mapped[list] = mapped_column(JSON, nullable=False, default=list)


# ApprovalTask.kind / status
APPROVAL_KINDS = ("CRB", "Legal")
APPROVAL_STATUSES = ("Pending", "Approved", "Rejected")


class ApprovalTask(Base):
    """A human-gated recommendation (CRB listing / legal handoff). Nothing that
    removes discretion is auto-fired — a Manager approves or rejects each one."""

    __tablename__ = "approval_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    loan_no: Mapped[str] = mapped_column(ForeignKey("loans.loan_no"), nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False, default="CRB")
    reason: Mapped[str] = mapped_column(String, nullable=False, default="")
    status: Mapped[str] = mapped_column(String, nullable=False, default="Pending")
    event_id: Mapped[int | None] = mapped_column(ForeignKey("automation_events.id"), nullable=True)
    rule_id: Mapped[int | None] = mapped_column(ForeignKey("automation_rules.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String, nullable=True)
    decision_reason: Mapped[str | None] = mapped_column(String, nullable=True)

    # Idempotency: one open CRB/Legal task per loan+kind at a time.
    dedupe_key: Mapped[str] = mapped_column(String, nullable=False, default="")


class CrbSubmission(Base):
    """A stubbed CRB bureau submission. Only ever created after a Manager
    approves the corresponding ApprovalTask — never auto-fired."""

    __tablename__ = "crb_submissions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    loan_no: Mapped[str] = mapped_column(ForeignKey("loans.loan_no"), nullable=False)
    approval_task_id: Mapped[int | None] = mapped_column(
        ForeignKey("approval_tasks.id"), nullable=True
    )
    bureau: Mapped[str] = mapped_column(String, nullable=False, default="Metropol")
    reference: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="Submitted")
    approved_by: Mapped[str | None] = mapped_column(String, nullable=True)
    submitted_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
