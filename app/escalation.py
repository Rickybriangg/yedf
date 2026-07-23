"""Milestone 3 — the auto-escalation state machine.

Each RecoveryCase walks the EscalationPath configured for its tier. The nightly
job advances a case to the next stage once the configured day-offset has elapsed
*and* no payment/response has been logged since it entered the current stage. A
case that sits in one stage 2x longer than its dwell is flagged for a human
(never auto-escalated past that point). A logged payment stops advancement and
marks the case to be reviewed for closure.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.gateways import sms_gateway
from app.models import (
    CONTACT_CHANNELS,
    RESPONSE_ACTION_TYPES,
    AutomationEvent,
    EscalationPath,
    Loan,
    PaymentLink,
    RecoveryAction,
    RecoveryCase,
)
from app.templating import loan_merge_context, render
from app import crb

_DEFAULT_STAGE_BODY = (
    "Hi {{borrowerName}}, regarding loan {{loanNo}} (balance KES "
    "{{outstandingBalance}}, due {{dueDate}}): please get in touch to resolve."
)


def _path_for(session: Session, tier: str | None) -> Optional[EscalationPath]:
    if not tier:
        return None
    return session.execute(
        select(EscalationPath).where(EscalationPath.tier == tier)
    ).scalars().first()


def _has_response_since(session: Session, case: RecoveryCase, since: date) -> bool:
    """A payment or logged borrower response on/after `since` stops advancement."""
    act = session.execute(
        select(RecoveryAction).where(
            RecoveryAction.loan_no == case.loan_no,
            RecoveryAction.type.in_(tuple(RESPONSE_ACTION_TYPES)),
            RecoveryAction.created_at >= since,
        )
    ).scalars().first()
    if act is not None:
        return True
    paid = session.execute(
        select(PaymentLink).where(
            PaymentLink.loan_no == case.loan_no,
            PaymentLink.status == "Paid",
        )
    ).scalars().all()
    return any(p.paid_at and p.paid_at.date() >= since for p in paid)


def _perform_stage_action(
    session: Session,
    case: RecoveryCase,
    loan: Loan,
    stage: dict[str, Any],
    stage_index: int,
    today: date,
) -> None:
    """Execute one escalation stage's action and log an audit event."""
    action = stage.get("action", "Notify")
    channel = stage.get("channel", "InApp")
    label = stage.get("label", action)
    triggered_at = datetime.combine(today, datetime.min.time())
    key = f"esc:{case.id}:{case.path_entered_at}:{stage_index}"

    # Idempotency: never perform the same stage twice.
    if session.execute(
        select(AutomationEvent).where(AutomationEvent.trigger_key == key)
    ).scalars().first() is not None:
        return

    reason = f"Escalation stage {stage_index} ({label}) for case #{case.id} [{loan.recovery_tier}]"
    status = "Scheduled"
    payload: dict[str, Any] = {"stage": label, "stage_index": stage_index}

    is_contact = channel in CONTACT_CHANNELS
    if is_contact and loan.do_not_contact:
        status = "Skipped"
        reason = f"Opt-out (doNotContact) — {reason}"
    elif action in ("SendReminder", "SMS", "USSD"):
        rendered = render(_DEFAULT_STAGE_BODY, loan_merge_context(loan))
        result = sms_gateway.send(
            session, loan_no=loan.loan_no, to_addr=loan.borrower_phone,
            channel=channel, body=rendered, event_id=None,
        )
        status = "Sent" if result.ok else "Failed"
        payload["rendered"] = rendered
    elif action in ("CRB-recommend", "FlagForCRB"):
        crb.create_approval_task(session, loan_no=loan.loan_no, reason=reason, kind="CRB")
        reason = f"Pending manager approval (CRB) — {reason}"
    elif action in ("LegalHandoff-recommend", "LegalHandoff"):
        crb.create_approval_task(session, loan_no=loan.loan_no, reason=reason, kind="Legal")
        reason = f"Pending manager approval (Legal) — {reason}"
    elif action in ("Notify", "Call-task", "GuarantorContact"):
        status = "Sent"  # officer task raised in-app
    # GenerateLetter / others -> Scheduled (letter batch)

    session.add(
        AutomationEvent(
            loan_no=loan.loan_no, rule_id=None, source="escalation",
            triggered_at=triggered_at, action=action, channel=channel,
            payload=payload, status=status, reason=reason, trigger_key=key,
        )
    )


def advance_cases(session: Session, today: date, summary: dict[str, Any]) -> None:
    """Advance every open case along its path. Mutates `summary` counters."""
    summary.setdefault("cases_advanced", 0)
    summary.setdefault("cases_entered", 0)
    summary.setdefault("cases_review_payment", 0)
    summary.setdefault("cases_stalled", 0)

    cases = list(session.execute(select(RecoveryCase)).scalars())
    for case in cases:
        loan = session.get(Loan, case.loan_no)
        if loan is None or loan.automation_paused:
            continue
        if case.status != "Open":
            continue

        tier = loan.recovery_tier
        path = _path_for(session, tier)
        if path is None or not path.stages:
            continue
        stages = path.stages

        # Enter the path (or re-enter on tier change) at stage 0.
        if case.current_stage_index is None or case.tier != tier:
            case.tier = tier
            case.path_entered_at = today
            case.current_stage_index = 0
            case.entered_stage_at = today
            case.current_stage = stages[0].get("label", stages[0].get("action"))
            _perform_stage_action(session, case, loan, stages[0], 0, today)
            summary["cases_entered"] += 1
            continue

        # A payment/response since entering the stage stops advancement.
        if _has_response_since(session, case, case.entered_stage_at):
            case.status = "ReviewToClose"
            case.flagged_for_review = True
            case.review_reason = "Payment/response logged — review to close"
            summary["cases_review_payment"] += 1
            continue

        idx = case.current_stage_index
        time_in_stage = (today - case.entered_stage_at).days

        if idx + 1 < len(stages):
            dwell = stages[idx + 1].get("offsetDays", 0) - stages[idx].get("offsetDays", 0)
            dwell = max(dwell, 1)
            if time_in_stage >= dwell:
                nxt = idx + 1
                case.current_stage_index = nxt
                case.entered_stage_at = today
                case.current_stage = stages[nxt].get("label", stages[nxt].get("action"))
                _perform_stage_action(session, case, loan, stages[nxt], nxt, today)
                summary["cases_advanced"] += 1
            elif time_in_stage >= 2 * dwell:
                _flag_stalled(case, summary, dwell, time_in_stage)
        else:
            # Final stage: stall threshold from the last dwell (fallback 7d).
            prev_off = stages[idx - 1].get("offsetDays", 0) if idx > 0 else 0
            dwell = max(stages[idx].get("offsetDays", 0) - prev_off, 7)
            if time_in_stage >= 2 * dwell:
                _flag_stalled(case, summary, dwell, time_in_stage)


def _flag_stalled(case: RecoveryCase, summary: dict, dwell: int, time_in_stage: int) -> None:
    if not case.flagged_for_review:
        case.flagged_for_review = True
        case.review_reason = (
            f"Stalled — {time_in_stage}d in stage (2x the {dwell}d dwell); manager review"
        )
        summary["cases_stalled"] += 1
