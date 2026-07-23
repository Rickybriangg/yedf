"""Milestone 7 — automation dashboard aggregation.

One call returns everything the dashboard screen needs: rules with last-fired
counts, scheduled sends today/this week, success/failure by channel, pending
approvals, paused loans, and the KPI estimate of manual hours automated.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    ApprovalTask,
    AutomationEvent,
    AutomationRule,
    Loan,
    RecoveryCase,
)

# Default effort assumption for the KPI (editable per call).
DEFAULT_MINUTES_PER_ITEM = 4


def _dt(d: date) -> datetime:
    return datetime.combine(d, datetime.min.time())


def summary(
    session: Session,
    today: Optional[date] = None,
    minutes_per_item: int = DEFAULT_MINUTES_PER_ITEM,
) -> dict[str, Any]:
    today = today or date.today()
    week_start = today - timedelta(days=today.weekday())  # Monday
    week_end = week_start + timedelta(days=7)

    # Rules with last-fired counts.
    rules_out = []
    for r in session.execute(select(AutomationRule).order_by(AutomationRule.id)).scalars():
        fired = session.execute(
            select(func.count(AutomationEvent.id)).where(AutomationEvent.rule_id == r.id)
        ).scalar_one()
        rules_out.append(
            {
                "id": r.id, "name": r.name, "active": r.active,
                "action": r.action, "channel": r.channel,
                "requiresApproval": r.requires_approval, "firedCount": fired,
            }
        )

    # Scheduled sends today / this week.
    def _count_between(start: datetime, end: datetime) -> int:
        return session.execute(
            select(func.count(AutomationEvent.id)).where(
                AutomationEvent.triggered_at >= start, AutomationEvent.triggered_at < end
            )
        ).scalar_one()

    scheduled_today = _count_between(_dt(today), _dt(today + timedelta(days=1)))
    scheduled_week = _count_between(_dt(week_start), _dt(week_end))

    # Success/failure by channel.
    by_channel: dict[str, dict[str, int]] = {}
    rows = session.execute(
        select(AutomationEvent.channel, AutomationEvent.status, func.count(AutomationEvent.id))
        .group_by(AutomationEvent.channel, AutomationEvent.status)
    ).all()
    for channel, status, n in rows:
        by_channel.setdefault(channel, {})[status] = n

    # Pending approvals (CRB / legal).
    pending = [
        {"id": t.id, "loanNo": t.loan_no, "kind": t.kind, "reason": t.reason,
         "createdAt": t.created_at.isoformat()}
        for t in session.execute(
            select(ApprovalTask).where(ApprovalTask.status == "Pending").order_by(ApprovalTask.id)
        ).scalars()
    ]

    # Paused loans.
    paused = [
        {"loanNo": l.loan_no, "borrowerName": l.borrower_name, "tier": l.recovery_tier}
        for l in session.execute(
            select(Loan).where(Loan.automation_paused.is_(True)).order_by(Loan.loan_no)
        ).scalars()
    ]

    # Cases flagged for review.
    flagged = session.execute(
        select(func.count(RecoveryCase.id)).where(RecoveryCase.flagged_for_review.is_(True))
    ).scalar_one()

    # KPI: hours of manual work automated this week (ESTIMATE).
    automated_items = session.execute(
        select(func.count(AutomationEvent.id)).where(
            AutomationEvent.triggered_at >= _dt(week_start),
            AutomationEvent.triggered_at < _dt(week_end),
            AutomationEvent.status.in_(("Sent", "Scheduled")),
            AutomationEvent.action.in_(("SendReminder", "SendPaymentLink", "GenerateLetter")),
        )
    ).scalar_one()
    hours = round(automated_items * minutes_per_item / 60.0, 1)

    return {
        "asOf": today.isoformat(),
        "rules": rules_out,
        "scheduledToday": scheduled_today,
        "scheduledThisWeek": scheduled_week,
        "byChannel": by_channel,
        "pendingApprovals": pending,
        "pausedLoans": paused,
        "casesFlaggedForReview": flagged,
        "kpi": {
            "label": "Hours of manual work automated this week (estimate)",
            "isEstimate": True,
            "itemsAutomated": automated_items,
            "minutesPerItem": minutes_per_item,
            "hours": hours,
        },
    }


def event_log(
    session: Session,
    q: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    stmt = select(AutomationEvent).order_by(
        AutomationEvent.triggered_at.desc(), AutomationEvent.id.desc()
    )
    if status:
        stmt = stmt.where(AutomationEvent.status == status)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            AutomationEvent.loan_no.like(like) | AutomationEvent.reason.like(like)
        )
    stmt = stmt.limit(limit)
    return [
        {
            "id": e.id, "loanNo": e.loan_no, "ruleId": e.rule_id, "source": e.source,
            "action": e.action, "channel": e.channel, "status": e.status,
            "reason": e.reason, "triggeredAt": e.triggered_at.isoformat(),
        }
        for e in session.execute(stmt).scalars()
    ]
