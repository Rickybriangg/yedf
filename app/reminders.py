"""Milestone 2 — reminder preview + calendar.

preview() answers the acceptance question directly: for a chosen date, exactly
which loans will receive a reminder and the exact rendered message text, WITHOUT
sending anything (nothing is written to the DB). calendar() shows what is
scheduled/sent over a window, grouped by tier and product.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.automation import compute_facts, retier
from app.gateways import StubPaymentProvider
from app.models import CONTACT_CHANNELS, AutomationEvent, AutomationRule, Loan, MessageTemplate
from app.rule_engine import ConditionError, match_condition
from app.templating import loan_merge_context, render

_REMINDER_ACTIONS = {"SendReminder", "SendPaymentLink"}
_DEFAULT_REMINDER = (
    "Hi {{borrowerName}}, loan {{loanNo}} has an outstanding balance of "
    "KES {{outstandingBalance}} due {{dueDate}}. Please pay to avoid escalation."
)


def preview(session: Session, run_date: Optional[date] = None, base_url: str = "") -> dict[str, Any]:
    """Dry-run: what reminders WOULD send on run_date, with rendered text.

    Read-only — re-tiers in memory only and never commits.
    """
    today = run_date or date.today()
    loans = list(session.execute(select(Loan)).scalars())
    for loan in loans:
        retier(loan, today)  # in-memory refresh; caller does not commit

    rules = [
        r for r in session.execute(
            select(AutomationRule).where(AutomationRule.active.is_(True))
        ).scalars()
        if r.action in _REMINDER_ACTIONS
    ]

    items: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for loan in loans:
        if loan.automation_paused:
            skipped.append({"loanNo": loan.loan_no, "reason": "automationPaused"})
            continue
        facts = compute_facts(loan, today)
        for rule in rules:
            try:
                if not match_condition(facts, rule.condition):
                    continue
            except ConditionError:
                continue

            if rule.channel in CONTACT_CHANNELS and loan.do_not_contact:
                skipped.append(
                    {"loanNo": loan.loan_no, "rule": rule.name, "reason": "doNotContact"}
                )
                continue

            tpl = session.get(MessageTemplate, rule.template_id) if rule.template_id else None
            body_src = tpl.body if tpl else _DEFAULT_REMINDER
            pay_url = f"{base_url.rstrip('/')}/pay/PREVIEW" if rule.action == "SendPaymentLink" else None
            rendered = render(body_src, loan_merge_context(loan, payment_link=pay_url))
            items.append(
                {
                    "loanNo": loan.loan_no,
                    "borrowerName": loan.borrower_name,
                    "phone": loan.borrower_phone,
                    "tier": loan.recovery_tier,
                    "product": loan.product,
                    "channel": rule.channel,
                    "rule": rule.name,
                    "message": rendered,
                }
            )

    # Read-only guarantee: drop any in-memory tier changes.
    session.rollback()
    return {"run_date": today.isoformat(), "count": len(items), "reminders": items, "skipped": skipped}


def calendar(
    session: Session, from_date: date, to_date: date
) -> dict[str, Any]:
    """Reminder events scheduled/sent within [from_date, to_date], grouped."""
    start = datetime_min(from_date)
    end = datetime_min(to_date + timedelta(days=1))
    events = list(
        session.execute(
            select(AutomationEvent).where(
                AutomationEvent.action.in_(tuple(_REMINDER_ACTIONS)),
                AutomationEvent.triggered_at >= start,
                AutomationEvent.triggered_at < end,
            )
        ).scalars()
    )

    by_tier: dict[str, int] = {}
    by_product: dict[str, int] = {}
    by_day: dict[str, int] = {}
    for e in events:
        loan = session.get(Loan, e.loan_no)
        tier = (loan.recovery_tier if loan else None) or "Unknown"
        product = (loan.product if loan else None) or "Unknown"
        day = e.triggered_at.date().isoformat()
        by_tier[tier] = by_tier.get(tier, 0) + 1
        by_product[product] = by_product.get(product, 0) + 1
        by_day[day] = by_day.get(day, 0) + 1

    return {
        "from": from_date.isoformat(),
        "to": to_date.isoformat(),
        "total": len(events),
        "by_tier": by_tier,
        "by_product": by_product,
        "by_day": by_day,
    }


def datetime_min(d: date):
    from datetime import datetime

    return datetime.combine(d, datetime.min.time())
