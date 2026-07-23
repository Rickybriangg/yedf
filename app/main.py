"""FastAPI app exposing the full RecoverIQ automation layer (Milestones 1–7).

Everything runs offline — no external SMS/payment/CRB provider is wired; every
external call sits behind an interface with a working stub.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date, timedelta
from typing import Any, Optional

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import batch as batch_mod
from app import crb as crb_mod
from app import dashboard as dash_mod
from app import payments as pay_mod
from app import reminders as rem_mod
from app.automation import run_daily
from app.database import get_db, init_db
from app.models import (
    ApprovalTask,
    AutomationRule,
    CrbSubmission,
    Loan,
    MessageTemplate,
    PaymentLink,
    RecoveryAction,
    RecoveryCase,
)
from app.templating import loan_merge_context, merge_fields, render


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="RecoverIQ Automation Layer", version="1.0.0", lifespan=lifespan)


def _parse_date(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")


# --------------------------------------------------------------------------- #
# Serializers
# --------------------------------------------------------------------------- #
def _loan_dict(loan: Loan) -> dict:
    return {
        "loanNo": loan.loan_no, "borrowerName": loan.borrower_name,
        "product": loan.product, "outstandingBalance": loan.outstanding_balance,
        "dueDate": loan.due_date.isoformat(), "recoveryTier": loan.recovery_tier,
        "arrearsBucket": loan.arrears_bucket, "dormancyDays": loan.dormancy_days,
        "automationPaused": loan.automation_paused, "doNotContact": loan.do_not_contact,
    }


def _rule_dict(r: AutomationRule) -> dict:
    return {
        "id": r.id, "name": r.name, "active": r.active, "condition": r.condition,
        "action": r.action, "channel": r.channel, "templateId": r.template_id,
        "cooldownDays": r.cooldown_days, "requiresApproval": r.requires_approval,
    }


def _case_dict(c: RecoveryCase) -> dict:
    return {
        "id": c.id, "loanNo": c.loan_no, "status": c.status, "tier": c.tier,
        "currentStage": c.current_stage, "currentStageIndex": c.current_stage_index,
        "enteredStageAt": c.entered_stage_at.isoformat() if c.entered_stage_at else None,
        "pathEnteredAt": c.path_entered_at.isoformat() if c.path_entered_at else None,
        "flaggedForReview": c.flagged_for_review, "reviewReason": c.review_reason,
    }


def _tpl_dict(t: MessageTemplate) -> dict:
    return {
        "id": t.id, "name": t.name, "channel": t.channel, "subject": t.subject,
        "body": t.body, "language": t.language, "mergeFields": merge_fields(t.body),
    }


# --------------------------------------------------------------------------- #
# Core / Milestone 1
# --------------------------------------------------------------------------- #
@app.post("/api/automation/run-daily")
def api_run_daily(
    request: Request,
    date_str: Optional[str] = Query(default=None, alias="date"),
    db: Session = Depends(get_db),
):
    return run_daily(db, run_date=_parse_date(date_str), base_url=str(request.base_url))


@app.get("/api/automation/rules")
def api_rules(db: Session = Depends(get_db)):
    return [_rule_dict(r) for r in db.execute(select(AutomationRule).order_by(AutomationRule.id)).scalars()]


@app.get("/api/loans")
def api_loans(db: Session = Depends(get_db)):
    return [_loan_dict(l) for l in db.execute(select(Loan).order_by(Loan.loan_no)).scalars()]


@app.post("/api/loans/{loan_no}/pause")
def api_pause(loan_no: str, paused: bool = Query(default=True), db: Session = Depends(get_db)):
    loan = db.get(Loan, loan_no)
    if not loan:
        raise HTTPException(404, "unknown loan")
    loan.automation_paused = paused
    db.commit()
    return {"loanNo": loan_no, "automationPaused": loan.automation_paused}


# --------------------------------------------------------------------------- #
# Milestone 2 — reminders
# --------------------------------------------------------------------------- #
@app.get("/api/reminders/preview")
def api_reminder_preview(
    request: Request,
    date_str: Optional[str] = Query(default=None, alias="date"),
    db: Session = Depends(get_db),
):
    return rem_mod.preview(db, run_date=_parse_date(date_str), base_url=str(request.base_url))


@app.get("/api/reminders/calendar")
def api_reminder_calendar(
    from_str: Optional[str] = Query(default=None, alias="from"),
    to_str: Optional[str] = Query(default=None, alias="to"),
    db: Session = Depends(get_db),
):
    frm = _parse_date(from_str) or date.today()
    to = _parse_date(to_str) or (frm + timedelta(days=7))
    return rem_mod.calendar(db, frm, to)


# --------------------------------------------------------------------------- #
# Milestone 3 — cases / escalation
# --------------------------------------------------------------------------- #
@app.get("/api/cases")
def api_cases(db: Session = Depends(get_db)):
    return [_case_dict(c) for c in db.execute(select(RecoveryCase).order_by(RecoveryCase.id)).scalars()]


@app.post("/api/actions")
def api_log_action(
    payload: dict = Body(...),
    db: Session = Depends(get_db),
):
    loan_no = payload.get("loanNo")
    loan = db.get(Loan, loan_no) if loan_no else None
    if not loan:
        raise HTTPException(404, "unknown loan")
    case = db.execute(select(RecoveryCase).where(RecoveryCase.loan_no == loan_no)).scalars().first()
    action = RecoveryAction(
        loan_no=loan_no, case_id=case.id if case else None,
        type=payload.get("type", "Borrower responded"), note=payload.get("note", ""),
        created_at=_parse_date(payload.get("date")) or date.today(),
    )
    db.add(action)
    db.commit()
    return {"ok": True, "id": action.id}


# --------------------------------------------------------------------------- #
# Milestone 4 — templates + batch generation
# --------------------------------------------------------------------------- #
@app.get("/api/templates")
def api_templates(db: Session = Depends(get_db)):
    return [_tpl_dict(t) for t in db.execute(select(MessageTemplate).order_by(MessageTemplate.id)).scalars()]


@app.post("/api/templates")
def api_create_template(payload: dict = Body(...), db: Session = Depends(get_db)):
    t = MessageTemplate(
        name=payload["name"], channel=payload.get("channel", "SMS"),
        subject=payload.get("subject"), body=payload["body"],
        language=payload.get("language", "en"),
    )
    db.add(t)
    db.commit()
    return _tpl_dict(t)


@app.put("/api/templates/{template_id}")
def api_update_template(template_id: int, payload: dict = Body(...), db: Session = Depends(get_db)):
    t = db.get(MessageTemplate, template_id)
    if not t:
        raise HTTPException(404, "unknown template")
    for field in ("name", "channel", "subject", "body", "language"):
        if field in payload:
            setattr(t, field, payload[field])
    db.commit()
    return _tpl_dict(t)


@app.delete("/api/templates/{template_id}")
def api_delete_template(template_id: int, db: Session = Depends(get_db)):
    t = db.get(MessageTemplate, template_id)
    if not t:
        raise HTTPException(404, "unknown template")
    db.delete(t)
    db.commit()
    return {"ok": True}


@app.post("/api/templates/{template_id}/preview")
def api_preview_template(
    template_id: int,
    loan_no: Optional[str] = Query(default=None, alias="loanNo"),
    db: Session = Depends(get_db),
):
    t = db.get(MessageTemplate, template_id)
    if not t:
        raise HTTPException(404, "unknown template")
    loan = db.get(Loan, loan_no) if loan_no else db.execute(select(Loan)).scalars().first()
    if not loan:
        raise HTTPException(404, "no loan to preview against")
    ctx = loan_merge_context(loan, payment_link=f"{str_base(db)}/pay/PREVIEW")
    return {
        "loanNo": loan.loan_no,
        "subject": render(t.subject, ctx) if t.subject else None,
        "body": render(t.body, ctx),
    }


def str_base(db) -> str:  # placeholder base for preview links
    return ""


@app.post("/api/batch/letters")
def api_batch_letters(request: Request, payload: dict = Body(default={}), db: Session = Depends(get_db)):
    loan_nos = payload.get("loanNos")
    if not loan_nos:
        loan_nos = [l.loan_no for l in batch_mod.worklist(db, tier=payload.get("tier"))]
    return batch_mod.generate_letter_batch(
        db, loan_nos, template_id=payload.get("templateId"),
        run_date=_parse_date(payload.get("date")),
    )


@app.post("/api/batch/sms")
def api_batch_sms(payload: dict = Body(default={}), db: Session = Depends(get_db)):
    loan_nos = payload.get("loanNos")
    if not loan_nos:
        loan_nos = [l.loan_no for l in batch_mod.worklist(db, tier=payload.get("tier"))]
    return batch_mod.generate_sms_csv(
        db, loan_nos, template_id=payload.get("templateId"),
        run_date=_parse_date(payload.get("date")),
    )


# --------------------------------------------------------------------------- #
# Milestone 5 — payments
# --------------------------------------------------------------------------- #
@app.post("/api/payments/link")
def api_create_link(request: Request, payload: dict = Body(...), db: Session = Depends(get_db)):
    loan = db.get(Loan, payload.get("loanNo"))
    if not loan:
        raise HTTPException(404, "unknown loan")
    link, url = pay_mod.create_payment_link(db, loan, amount=payload.get("amount"), base_url=str(request.base_url))
    db.commit()
    return {"id": link.id, "loanNo": link.loan_no, "amount": link.amount, "token": link.token, "payUrl": url, "status": link.status}


@app.post("/api/payments/callback")
def api_payment_callback(payload: dict = Body(...), db: Session = Depends(get_db)):
    """Webhook-style callback that marks a PaymentLink Paid (dev-triggerable)."""
    token = payload.get("token")
    if not token:
        raise HTTPException(400, "token required")
    result = pay_mod.mark_paid(db, token, external_ref=payload.get("externalRef"))
    if not result.get("ok"):
        raise HTTPException(404, result.get("error", "callback failed"))
    return result


@app.get("/pay/{token}")
def api_simulate_pay(token: str, db: Session = Depends(get_db)):
    """Dev convenience: visiting the pay link simulates the borrower paying."""
    result = pay_mod.mark_paid(db, token)
    if not result.get("ok"):
        raise HTTPException(404, result.get("error", "unknown token"))
    return result


@app.get("/api/payments")
def api_list_payments(db: Session = Depends(get_db)):
    return [
        {"id": p.id, "loanNo": p.loan_no, "amount": p.amount, "status": p.status,
         "provider": p.provider, "externalRef": p.external_ref,
         "paidAt": p.paid_at.isoformat() if p.paid_at else None}
        for p in db.execute(select(PaymentLink).order_by(PaymentLink.id)).scalars()
    ]


# --------------------------------------------------------------------------- #
# Milestone 6 — CRB approvals (recommend-only)
# --------------------------------------------------------------------------- #
@app.get("/api/approvals")
def api_approvals(
    status: Optional[str] = Query(default="Pending"),
    db: Session = Depends(get_db),
):
    stmt = select(ApprovalTask).order_by(ApprovalTask.id)
    if status:
        stmt = stmt.where(ApprovalTask.status == status)
    return [
        {"id": t.id, "loanNo": t.loan_no, "kind": t.kind, "reason": t.reason,
         "status": t.status, "createdAt": t.created_at.isoformat(),
         "decidedBy": t.decided_by, "decisionReason": t.decision_reason}
        for t in db.execute(stmt).scalars()
    ]


@app.post("/api/approvals/{task_id}/approve")
def api_approve(task_id: int, payload: dict = Body(default={}), db: Session = Depends(get_db)):
    decided_by = payload.get("decidedBy") or "manager"
    result = crb_mod.decide(db, task_id, approve=True, decided_by=decided_by, reason=payload.get("reason"))
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "cannot approve"))
    return result


@app.post("/api/approvals/{task_id}/reject")
def api_reject(task_id: int, payload: dict = Body(default={}), db: Session = Depends(get_db)):
    decided_by = payload.get("decidedBy") or "manager"
    result = crb_mod.decide(db, task_id, approve=False, decided_by=decided_by, reason=payload.get("reason"))
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "cannot reject"))
    return result


@app.get("/api/crb/submissions")
def api_crb_submissions(db: Session = Depends(get_db)):
    return [
        {"id": s.id, "loanNo": s.loan_no, "bureau": s.bureau, "reference": s.reference,
         "status": s.status, "approvedBy": s.approved_by, "submittedAt": s.submitted_at.isoformat()}
        for s in db.execute(select(CrbSubmission).order_by(CrbSubmission.id)).scalars()
    ]


# --------------------------------------------------------------------------- #
# Milestone 7 — dashboard
# --------------------------------------------------------------------------- #
@app.get("/api/dashboard")
def api_dashboard(
    date_str: Optional[str] = Query(default=None, alias="date"),
    minutes_per_item: int = Query(default=dash_mod.DEFAULT_MINUTES_PER_ITEM, alias="minutesPerItem"),
    db: Session = Depends(get_db),
):
    return dash_mod.summary(db, today=_parse_date(date_str), minutes_per_item=minutes_per_item)


@app.get("/api/automation/events")
def api_events(
    q: Optional[str] = Query(default=None),
    status: Optional[str] = None,
    limit: int = Query(default=200, le=1000),
    db: Session = Depends(get_db),
):
    return dash_mod.event_log(db, q=q, status=status, limit=limit)


# --------------------------------------------------------------------------- #
# Dev helpers
# --------------------------------------------------------------------------- #
@app.post("/api/dev/seed")
def api_seed(db: Session = Depends(get_db)):
    from app.seed import seed

    seed(db)
    return {"seeded_loans": len(db.execute(select(Loan)).scalars().all())}


@app.get("/", response_class=HTMLResponse)
def index():
    return (
        "<h1>RecoverIQ Automation Layer</h1>"
        "<p>API is running. See <a href='/docs'>/docs</a> for the interactive API, "
        "or the live client-side dashboard demo on GitHub Pages.</p>"
    )
