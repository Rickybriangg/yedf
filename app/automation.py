"""Milestone 1 — the nightly re-tiering + rule-evaluation job.

run_daily():
  1. Recomputes each loan's recovery tier / arrears bucket / dormancy (reusing
     tiering.py — the single source of truth).
  2. Evaluates every active rule against every loan that is not paused.
  3. For matching loans (respecting cooldown), creates an AutomationEvent.

Guarantees:
  * Idempotent — running twice on the same data produces the same event count
    the second time (trigger_key dedup + a UNIQUE backstop in the DB).
  * Paused loans are skipped silently.
  * doNotContact is checked right before "sending" a contact-channel action, so
    a change since rule-match time is still honoured.
  * Everything that fires is logged with the rule id and a human reason.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import tiering
from app.models import (
    CONTACT_CHANNELS,
    AutomationEvent,
    AutomationRule,
    Loan,
)
from app.rule_engine import ConditionError, match_condition


def retier(loan: Loan, today: date) -> None:
    """Refresh a loan's derived recovery fields. Single source of truth =
    tiering.py; we do not re-implement tier rules here."""
    dpd = tiering.days_past_due(loan.due_date, today)
    loan.recovery_tier = tiering.recovery_tier(dpd)
    loan.arrears_bucket = tiering.arrears_bucket(dpd)
    loan.dormancy_days = tiering.dormancy_days(
        loan.last_payment_date, loan.disbursed_date, today
    )


def compute_facts(loan: Loan, today: date) -> dict[str, Any]:
    """Build the fact dict the rule matcher evaluates. Keys match
    rule_engine.ALLOWED_FIELDS (camelCase, matching the condition JSON)."""
    return {
        "loanNo": loan.loan_no,
        "tier": loan.recovery_tier,
        "arrearsBucket": loan.arrears_bucket,
        "dormancyDays": loan.dormancy_days,
        "daysPastDue": tiering.days_past_due(loan.due_date, today),
        "daysSinceLastAction": tiering.days_since_last_action(
            loan.last_action_date, loan.disbursed_date, today
        ),
        "product": loan.product,
        "outstandingBalance": loan.outstanding_balance,
    }


def _explain(rule: AutomationRule, facts: dict[str, Any]) -> str:
    return (
        f"Matched rule '{rule.name}' [{rule.action}/{rule.channel}] — "
        f"tier={facts['tier']}, daysPastDue={facts['daysPastDue']}, "
        f"daysSinceLastAction={facts['daysSinceLastAction']}"
    )


def _last_fired(session: Session, rule_id: int, loan_no: str) -> Optional[AutomationEvent]:
    """Most recent event that actually 'counts' for cooldown (was scheduled or
    sent — skipped/cancelled events don't start a cooldown)."""
    stmt = (
        select(AutomationEvent)
        .where(
            AutomationEvent.rule_id == rule_id,
            AutomationEvent.loan_no == loan_no,
            AutomationEvent.status.in_(("Scheduled", "Sent")),
        )
        .order_by(AutomationEvent.triggered_at.desc())
        .limit(1)
    )
    return session.execute(stmt).scalars().first()


def _dispatch_status(rule: AutomationRule, loan: Loan, facts: dict[str, Any]) -> tuple[str, str]:
    """Decide the event's status + reason at the last possible moment.

    Milestone 1 has no live gateway (that is Milestone 2), so contact actions
    become 'Scheduled'. Two guardrails are applied here, right before send:
      * doNotContact on a contact channel  -> Skipped (opt-out respected).
      * requiresApproval (e.g. FlagForCRB)  -> Scheduled as a pending
        recommendation; it is never auto-fired.
    """
    reason = _explain(rule, facts)
    if rule.channel in CONTACT_CHANNELS and loan.do_not_contact:
        return "Skipped", f"Opt-out (doNotContact) — {reason}"
    if rule.requires_approval:
        return "Scheduled", f"Pending manager approval — {reason}"
    return "Scheduled", reason


def run_daily(session: Session, run_date: Optional[date] = None) -> dict[str, Any]:
    """Execute the nightly job. Returns a summary of what happened and why."""
    today = run_date or date.today()
    triggered_at = datetime.combine(today, datetime.min.time())

    summary: dict[str, Any] = {
        "run_date": today.isoformat(),
        "loans_evaluated": 0,
        "created": 0,
        "skipped_paused": 0,
        "skipped_cooldown": 0,
        "skipped_duplicate": 0,
        "by_status": {},
        "by_rule": {},
        "rule_errors": [],
    }

    loans = list(session.execute(select(Loan)).scalars())

    # Step 1 — re-tier everything first, so rule evaluation sees fresh values.
    for loan in loans:
        retier(loan, today)
    session.flush()

    rules = list(
        session.execute(select(AutomationRule).where(AutomationRule.active.is_(True))).scalars()
    )

    # Step 2/3 — evaluate rules and create events.
    for loan in loans:
        # Paused loans are skipped silently (no event) — see brief §2.
        if loan.automation_paused:
            summary["skipped_paused"] += 1
            continue

        summary["loans_evaluated"] += 1
        facts = compute_facts(loan, today)

        for rule in rules:
            try:
                matched = match_condition(facts, rule.condition)
            except ConditionError as exc:
                # One broken rule must not abort the run — isolate and record it.
                summary["rule_errors"].append({"rule_id": rule.id, "error": str(exc)})
                continue
            if not matched:
                continue

            # Idempotency: one event per rule/loan/run-date.
            trigger_key = f"{rule.id}:{loan.loan_no}:{today.isoformat()}"
            already = session.execute(
                select(AutomationEvent).where(AutomationEvent.trigger_key == trigger_key)
            ).scalars().first()
            if already is not None:
                summary["skipped_duplicate"] += 1
                continue

            # Cooldown: minimum gap before this rule may fire again on this loan.
            if rule.cooldown_days and rule.cooldown_days > 0:
                last = _last_fired(session, rule.id, loan.loan_no)
                if last is not None:
                    gap = (today - last.triggered_at.date()).days
                    if gap < rule.cooldown_days:
                        summary["skipped_cooldown"] += 1
                        continue

            status, reason = _dispatch_status(rule, loan, facts)
            event = AutomationEvent(
                loan_no=loan.loan_no,
                rule_id=rule.id,
                triggered_at=triggered_at,
                action=rule.action,
                channel=rule.channel,
                payload={"facts": facts, "template_id": rule.template_id},
                status=status,
                reason=reason,
                trigger_key=trigger_key,
            )
            session.add(event)
            session.flush()  # surface UNIQUE + make visible to in-run dedup checks

            summary["created"] += 1
            summary["by_status"][status] = summary["by_status"].get(status, 0) + 1
            summary["by_rule"][rule.name] = summary["by_rule"].get(rule.name, 0) + 1

    session.commit()
    return summary
