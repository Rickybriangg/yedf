"""FastAPI app exposing the Milestone 1 automation engine.

Endpoints (all offline / no external calls):
  POST /api/automation/run-daily     run the nightly job (optional ?date=)
  GET  /api/automation/events        the AutomationEvent audit log
  GET  /api/automation/rules         configured rules
  GET  /api/loans                    loans with their current derived tier
  POST /api/dev/seed                 load sample data (dev convenience)
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.automation import run_daily
from app.database import get_db, init_db
from app.models import AutomationEvent, AutomationRule, Loan


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="RecoverIQ Automation Layer", version="0.1.0", lifespan=lifespan)


def _loan_dict(loan: Loan) -> dict:
    return {
        "loanNo": loan.loan_no,
        "borrowerName": loan.borrower_name,
        "product": loan.product,
        "outstandingBalance": loan.outstanding_balance,
        "dueDate": loan.due_date.isoformat(),
        "recoveryTier": loan.recovery_tier,
        "arrearsBucket": loan.arrears_bucket,
        "dormancyDays": loan.dormancy_days,
        "automationPaused": loan.automation_paused,
        "doNotContact": loan.do_not_contact,
    }


def _event_dict(e: AutomationEvent) -> dict:
    return {
        "id": e.id,
        "loanNo": e.loan_no,
        "ruleId": e.rule_id,
        "triggeredAt": e.triggered_at.isoformat(),
        "action": e.action,
        "channel": e.channel,
        "status": e.status,
        "reason": e.reason,
        "payload": e.payload,
    }


def _rule_dict(r: AutomationRule) -> dict:
    return {
        "id": r.id,
        "name": r.name,
        "active": r.active,
        "condition": r.condition,
        "action": r.action,
        "channel": r.channel,
        "templateId": r.template_id,
        "cooldownDays": r.cooldown_days,
        "requiresApproval": r.requires_approval,
    }


@app.post("/api/automation/run-daily")
def api_run_daily(
    date_str: Optional[str] = Query(default=None, alias="date"),
    db: Session = Depends(get_db),
):
    """Run the nightly re-tiering + rule-evaluation job. `date` (YYYY-MM-DD)
    overrides 'today' for testing/backfill; omit for the real run date."""
    run_date: Optional[date] = None
    if date_str:
        try:
            run_date = date.fromisoformat(date_str)
        except ValueError:
            raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
    return run_daily(db, run_date=run_date)


@app.get("/api/automation/events")
def api_events(
    loan_no: Optional[str] = Query(default=None, alias="loanNo"),
    status: Optional[str] = None,
    limit: int = Query(default=200, le=1000),
    db: Session = Depends(get_db),
):
    stmt = select(AutomationEvent).order_by(AutomationEvent.triggered_at.desc(), AutomationEvent.id.desc())
    if loan_no:
        stmt = stmt.where(AutomationEvent.loan_no == loan_no)
    if status:
        stmt = stmt.where(AutomationEvent.status == status)
    stmt = stmt.limit(limit)
    return [_event_dict(e) for e in db.execute(stmt).scalars()]


@app.get("/api/automation/rules")
def api_rules(db: Session = Depends(get_db)):
    rules = db.execute(select(AutomationRule).order_by(AutomationRule.id)).scalars()
    return [_rule_dict(r) for r in rules]


@app.get("/api/loans")
def api_loans(db: Session = Depends(get_db)):
    loans = db.execute(select(Loan).order_by(Loan.loan_no)).scalars()
    return [_loan_dict(l) for l in loans]


@app.post("/api/dev/seed")
def api_seed(db: Session = Depends(get_db)):
    """Dev convenience: (re)load sample data."""
    from app.seed import seed

    seed(db)
    count = db.execute(select(Loan)).scalars().all()
    return {"seeded_loans": len(count)}
