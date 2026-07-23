"""Milestone 6 — CRB submission is recommend-only.

A FlagForCRB rule creates a pending ApprovalTask (never a live submission). A
Manager approves or rejects with one click; only on approval is a stubbed
CrbSubmission created and a Sent AutomationEvent logged. Every decision is
attributed and audited.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ApprovalTask, AutomationEvent, CrbSubmission


def create_approval_task(
    session: Session,
    *,
    loan_no: str,
    reason: str,
    kind: str = "CRB",
    event_id: Optional[int] = None,
    rule_id: Optional[int] = None,
) -> Optional[ApprovalTask]:
    """Create a pending approval task, unless an open one already exists for
    this loan+kind (idempotent — no duplicate manager tasks)."""
    dedupe_key = f"{kind}:{loan_no}:open"
    existing = session.execute(
        select(ApprovalTask).where(
            ApprovalTask.dedupe_key == dedupe_key,
            ApprovalTask.status == "Pending",
        )
    ).scalars().first()
    if existing is not None:
        return None

    task = ApprovalTask(
        loan_no=loan_no,
        kind=kind,
        reason=reason,
        status="Pending",
        event_id=event_id,
        rule_id=rule_id,
        created_at=datetime.utcnow(),
        dedupe_key=dedupe_key,
    )
    session.add(task)
    session.flush()
    return task


def decide(
    session: Session,
    task_id: int,
    *,
    approve: bool,
    decided_by: str,
    reason: Optional[str] = None,
) -> dict:
    """Approve or reject a pending task. On approve, create the stubbed
    submission + a Sent event. On reject, record the reason. Idempotent."""
    task = session.get(ApprovalTask, task_id)
    if task is None:
        return {"ok": False, "error": "unknown task"}
    if task.status != "Pending":
        return {"ok": False, "error": f"task already {task.status}"}

    now = datetime.utcnow()
    task.decided_at = now
    task.decided_by = decided_by
    task.decision_reason = reason
    # Open task consumed — clear the dedupe key so a future flag can re-open one.
    task.dedupe_key = f"{task.kind}:{task.loan_no}:{task.id}:decided"

    if not approve:
        task.status = "Rejected"
        session.add(
            AutomationEvent(
                loan_no=task.loan_no,
                rule_id=task.rule_id,
                source="crb",
                triggered_at=now,
                action="FlagForCRB",
                channel="InApp",
                payload={"approval_task_id": task.id, "decision": "Rejected"},
                status="Cancelled",
                reason=f"CRB recommendation rejected by {decided_by}"
                + (f": {reason}" if reason else ""),
                trigger_key=f"crb-reject:{task.id}",
            )
        )
        session.commit()
        return {"ok": True, "status": "Rejected", "loanNo": task.loan_no}

    # Approved — create the stubbed submission and a Sent audit event.
    task.status = "Approved"
    ref = f"CRB-{uuid.uuid4().hex[:10].upper()}"
    submission = CrbSubmission(
        loan_no=task.loan_no,
        approval_task_id=task.id,
        bureau="Metropol",
        reference=ref,
        status="Submitted",
        approved_by=decided_by,
        submitted_at=now,
    )
    session.add(submission)
    session.add(
        AutomationEvent(
            loan_no=task.loan_no,
            rule_id=task.rule_id,
            source="crb",
            triggered_at=now,
            action="FlagForCRB",
            channel="InApp",
            payload={"approval_task_id": task.id, "reference": ref, "decision": "Approved"},
            status="Sent",
            reason=f"CRB submission approved by {decided_by} — {ref}",
            trigger_key=f"crb-approve:{task.id}",
        )
    )
    session.commit()
    return {"ok": True, "status": "Approved", "loanNo": task.loan_no, "reference": ref}
