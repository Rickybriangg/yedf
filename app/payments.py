"""Milestone 5 — payment links + the stubbed M-Pesa callback.

When a link is marked Paid (via the dev callback), we log a RecoveryAction of
type 'Payment received' and stop further escalation on that loan's case.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.gateways import StubPaymentProvider, payment_provider
from app.models import (
    ACTION_PAYMENT,
    AutomationEvent,
    Loan,
    PaymentLink,
    RecoveryAction,
    RecoveryCase,
)


def create_payment_link(
    session: Session, loan: Loan, amount: Optional[float] = None, base_url: str = ""
) -> tuple[PaymentLink, str]:
    """Create a fresh payment link for a loan; returns (link, pay_url)."""
    amt = amount if amount is not None else loan.outstanding_balance
    link = payment_provider.create_stk_push(session, loan_no=loan.loan_no, amount=amt, base_url=base_url)
    url = StubPaymentProvider.pay_url(link, base_url)
    return link, url


def mark_paid(
    session: Session,
    token: str,
    external_ref: Optional[str] = None,
    when: Optional[date] = None,
) -> dict:
    """Callback handler: mark the link Paid, log a RecoveryAction, and stop
    escalation on the case. Idempotent — a second callback is a no-op."""
    link = session.execute(
        select(PaymentLink).where(PaymentLink.token == token)
    ).scalars().first()
    if link is None:
        return {"ok": False, "error": "unknown token"}

    if link.status == "Paid":
        return {"ok": True, "already_paid": True, "loanNo": link.loan_no}

    now = datetime.utcnow()
    action_date = when or now.date()
    link.status = "Paid"
    link.paid_at = now
    if external_ref:
        link.external_ref = external_ref

    case = session.execute(
        select(RecoveryCase).where(RecoveryCase.loan_no == link.loan_no)
    ).scalars().first()

    action = RecoveryAction(
        loan_no=link.loan_no,
        case_id=case.id if case else None,
        type=ACTION_PAYMENT,
        note=f"Payment received via {link.provider} ({link.external_ref}) — KES {link.amount:,.0f}",
        created_at=action_date,
    )
    session.add(action)

    stopped = False
    if case is not None and case.status == "Open":
        case.status = "ReviewToClose"
        case.flagged_for_review = True
        case.review_reason = "Payment received — escalation stopped, review to close"
        stopped = True

    # Audit event for the payment.
    session.add(
        AutomationEvent(
            loan_no=link.loan_no,
            rule_id=None,
            source="payment",
            triggered_at=now,
            action="Notify",
            channel="InApp",
            payload={"token": token, "amount": link.amount, "external_ref": link.external_ref},
            status="Sent",
            reason=f"Payment received — escalation stopped for {link.loan_no}",
            trigger_key=f"payment:{token}",
        )
    )
    session.commit()
    return {
        "ok": True,
        "loanNo": link.loan_no,
        "amount": link.amount,
        "escalation_stopped": stopped,
        "case_status": case.status if case else None,
    }
