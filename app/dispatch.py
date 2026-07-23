"""Turns a matched rule into a real (stubbed) action, at send time.

Called by the nightly job after an AutomationEvent row exists (so side effects
can link back to it). This is the "as late as possible" point where doNotContact
and requiresApproval are enforced — a change since rule-match time still counts.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import crb
from app.gateways import sms_gateway
from app.models import (
    CONTACT_CHANNELS,
    AutomationEvent,
    AutomationRule,
    Loan,
    MessageTemplate,
)
from app.payments import create_payment_link
from app.templating import loan_merge_context, render

_DEFAULT_REMINDER = (
    "Hi {{borrowerName}}, loan {{loanNo}} has an outstanding balance of "
    "KES {{outstandingBalance}} due {{dueDate}}. Please pay to avoid escalation."
)


def _template(session: Session, template_id: int | None) -> MessageTemplate | None:
    if template_id is None:
        return None
    return session.get(MessageTemplate, template_id)


def dispatch_rule_event(
    session: Session,
    rule: AutomationRule,
    loan: Loan,
    facts: dict[str, Any],
    event: AutomationEvent,
    base_url: str = "",
) -> None:
    """Perform the rule's action and set event.status / reason / payload.

    Mutates `event` in place; the caller commits.
    """
    reason = (
        f"Matched rule '{rule.name}' [{rule.action}/{rule.channel}] — "
        f"tier={facts['tier']}, daysPastDue={facts['daysPastDue']}, "
        f"daysSinceLastAction={facts['daysSinceLastAction']}"
    )
    is_contact = rule.channel in CONTACT_CHANNELS

    # Opt-out guardrail, checked at the last moment.
    if is_contact and loan.do_not_contact:
        event.status = "Skipped"
        event.reason = f"Opt-out (doNotContact) — {reason}"
        return

    # Recommend-only actions never auto-fire.
    if rule.action == "FlagForCRB" or rule.requires_approval:
        task = crb.create_approval_task(
            session,
            loan_no=loan.loan_no,
            reason=reason,
            kind="CRB" if rule.action == "FlagForCRB" else "Legal",
            event_id=event.id,
            rule_id=rule.id,
        )
        event.status = "Scheduled"
        event.reason = (
            f"Pending manager approval — {reason}"
            if task is not None
            else f"Pending manager approval (task already open) — {reason}"
        )
        return

    if rule.action in ("SendReminder", "SendPaymentLink"):
        pay_url = None
        payload_extra: dict[str, Any] = {}
        if rule.action == "SendPaymentLink":
            link, pay_url = create_payment_link(session, loan, base_url=base_url)
            payload_extra["payment_link_id"] = link.id
            payload_extra["payment_token"] = link.token

        tpl = _template(session, rule.template_id)
        body_src = tpl.body if tpl else _DEFAULT_REMINDER
        subject = tpl.subject if tpl else None
        ctx = loan_merge_context(loan, payment_link=pay_url)
        rendered = render(body_src, ctx)
        rendered_subject = render(subject, ctx) if subject else None

        result = sms_gateway.send(
            session,
            loan_no=loan.loan_no,
            to_addr=loan.borrower_phone,
            channel=rule.channel,
            body=rendered,
            subject=rendered_subject,
            template_id=rule.template_id,
            event_id=event.id,
        )
        event.status = "Sent" if result.ok else "Failed"
        event.reason = reason
        event.payload = {**event.payload, "rendered": rendered, "provider_ref": result.provider_ref, **payload_extra}
        return

    if rule.action == "GenerateLetter":
        # Letters are produced in officer-run batches (Milestone 4); the rule
        # just schedules one so it appears on the letter worklist.
        event.status = "Scheduled"
        event.reason = f"Queued for letter batch — {reason}"
        return

    if rule.action == "Notify":
        event.status = "Sent"
        event.reason = reason
        return

    if rule.action == "Escalate":
        # The escalation state machine owns stage advancement; this flags it.
        event.status = "Scheduled"
        event.reason = f"Escalation flagged — {reason}"
        return

    event.status = "Scheduled"
    event.reason = reason
