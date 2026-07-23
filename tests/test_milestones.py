"""Acceptance tests for Milestones 2–7."""
from datetime import timedelta

import pytest

from app import batch as batch_mod
from app import crb as crb_mod
from app import dashboard as dash_mod
from app import payments as pay_mod
from app import reminders as rem_mod
from app.automation import run_daily
from app.escalation import advance_cases
from app.gateways import ConsoleSmsGateway, StubPaymentProvider
from app.models import (
    ApprovalTask,
    AutomationEvent,
    CrbSubmission,
    EscalationPath,
    MessageTemplate,
    OutboundMessage,
    PaymentLink,
    RecoveryAction,
    RecoveryCase,
)
from app.templating import merge_fields, render
from tests.conftest import TODAY, d


# --------------------------------------------------------------------------- #
# Milestone 2 — reminder scheduler + gateway + preview
# --------------------------------------------------------------------------- #
def test_template_merge_fields_render():
    body = "Hi {{borrowerName}}, {{loanNo}} owes {{outstandingBalance}}."
    assert merge_fields(body) == ["borrowerName", "loanNo", "outstandingBalance"]
    out = render(body, {"borrowerName": "Amina", "loanNo": "L-1", "outstandingBalance": "1,000"})
    assert out == "Hi Amina, L-1 owes 1,000."


def test_gateway_logs_message_instead_of_sending(session, make_loan, make_rule):
    make_loan("L-1", borrower_phone="+254700")
    make_rule(action="SendReminder", channel="SMS", condition={"tier": "Curable"})
    run_daily(session, run_date=TODAY)
    msgs = session.query(OutboundMessage).all()
    assert len(msgs) == 1
    assert msgs[0].provider == ConsoleSmsGateway.name
    assert msgs[0].loan_no == "L-1"
    assert "L-1" in msgs[0].body


def test_reminder_preview_is_readonly_and_shows_text(session, make_loan, make_rule):
    tpl = MessageTemplate(name="t", channel="SMS",
                          body="Hi {{borrowerName}}, pay {{loanNo}}.", language="en")
    session.add(tpl); session.flush()
    make_loan("L-1", borrower_name="Amina")
    make_rule(action="SendReminder", channel="SMS", condition={"tier": "Curable"}, template_id=tpl.id)

    result = rem_mod.preview(session, run_date=TODAY)
    assert result["count"] == 1
    assert result["reminders"][0]["message"] == "Hi Amina, pay L-1."
    # Nothing was sent or persisted by preview.
    assert session.query(OutboundMessage).count() == 0
    assert session.query(AutomationEvent).count() == 0


def test_preview_respects_opt_out_and_pause(session, make_loan, make_rule):
    make_loan("L-dnc", do_not_contact=True)
    make_loan("L-paused", automation_paused=True)
    make_loan("L-ok")
    make_rule(action="SendReminder", channel="SMS", condition={"tier": "Curable"})
    result = rem_mod.preview(session, run_date=TODAY)
    assert {r["loanNo"] for r in result["reminders"]} == {"L-ok"}
    reasons = {s["reason"] for s in result["skipped"]}
    assert "doNotContact" in reasons and "automationPaused" in reasons


# --------------------------------------------------------------------------- #
# Milestone 3 — escalation state machine
# --------------------------------------------------------------------------- #
def _path(session, tier, stages):
    session.add(EscalationPath(tier=tier, stages=stages)); session.flush()


def _case(session, loan_no):
    c = RecoveryCase(loan_no=loan_no, status="Open")
    session.add(c); session.flush()
    return c


def test_case_advances_on_schedule_with_no_action(session, make_loan):
    make_loan("L-1", due_date=d(120))  # Doubtful
    _path(session, "Doubtful", [
        {"label": "Letter", "action": "GenerateLetter", "channel": "Letter", "offsetDays": 0},
        {"label": "Guarantor", "action": "GuarantorContact", "channel": "InApp", "offsetDays": 7},
    ])
    case = _case(session, "L-1")

    run_daily(session, run_date=TODAY)                       # enters stage 0
    session.refresh(case)
    assert case.current_stage_index == 0

    run_daily(session, run_date=TODAY + timedelta(days=7))   # 7d dwell elapsed
    session.refresh(case)
    assert case.current_stage_index == 1
    assert case.status == "Open"


def test_logged_payment_stops_escalation(session, make_loan):
    make_loan("L-1", due_date=d(120))
    _path(session, "Doubtful", [
        {"label": "Letter", "action": "GenerateLetter", "channel": "Letter", "offsetDays": 0},
        {"label": "Guarantor", "action": "GuarantorContact", "channel": "InApp", "offsetDays": 7},
    ])
    case = _case(session, "L-1")
    run_daily(session, run_date=TODAY)                       # enters stage 0

    # A payment lands before the next stage is due.
    session.add(RecoveryAction(loan_no="L-1", case_id=case.id,
                               type="Payment received", note="paid",
                               created_at=TODAY + timedelta(days=2)))
    session.commit()

    run_daily(session, run_date=TODAY + timedelta(days=7))
    session.refresh(case)
    assert case.current_stage_index == 0          # did not advance
    assert case.status == "ReviewToClose"
    assert case.flagged_for_review is True


def test_stalled_case_flagged_for_review(session, make_loan):
    make_loan("L-1", due_date=d(120))
    _path(session, "Doubtful", [
        {"label": "A", "action": "Notify", "channel": "InApp", "offsetDays": 0},
    ])  # single stage -> stall detection on the final stage
    case = _case(session, "L-1")
    run_daily(session, run_date=TODAY)            # enters stage 0
    # Sit well past 2x the fallback dwell (7d -> 14d).
    run_daily(session, run_date=TODAY + timedelta(days=20))
    session.refresh(case)
    assert case.flagged_for_review is True
    assert "Stalled" in (case.review_reason or "")


# --------------------------------------------------------------------------- #
# Milestone 4 — batch generation
# --------------------------------------------------------------------------- #
def test_letter_batch_produces_file_and_audit_per_account(session, make_loan, tmp_path):
    for i in range(5):
        make_loan(f"L-{i}", due_date=d(120))
    loan_nos = [f"L-{i}" for i in range(5)]
    result = batch_mod.generate_letter_batch(session, loan_nos, out_dir=str(tmp_path), run_date=TODAY)
    assert result["generated"] == 5
    # one audit event per account
    events = session.query(AutomationEvent).filter_by(source="batch", action="GenerateLetter").all()
    assert len(events) == 5
    # files exist + a zip
    assert (tmp_path / "L-0.pdf").exists() or (tmp_path / "L-0.txt").exists()
    assert result["zip"].endswith(".zip")


def test_sms_batch_csv_skips_opt_out(session, make_loan, tmp_path):
    make_loan("L-ok")
    make_loan("L-dnc", do_not_contact=True)
    out = tmp_path / "sms.csv"
    result = batch_mod.generate_sms_csv(session, ["L-ok", "L-dnc"], out_path=str(out), run_date=TODAY)
    assert result["rows"] == 1
    assert "L-ok" in result["content"] and "L-dnc" not in result["content"]


# --------------------------------------------------------------------------- #
# Milestone 5 — payment links + callback stops escalation
# --------------------------------------------------------------------------- #
def test_payment_callback_marks_paid_and_stops_escalation(session, make_loan):
    make_loan("L-1", due_date=d(120))
    _path(session, "Doubtful", [
        {"label": "Letter", "action": "GenerateLetter", "channel": "Letter", "offsetDays": 0},
        {"label": "Guarantor", "action": "GuarantorContact", "channel": "InApp", "offsetDays": 7},
    ])
    case = _case(session, "L-1")
    run_daily(session, run_date=TODAY)

    loan = session.get(type(case).loan.property.mapper.class_, "L-1") if False else None
    from app.models import Loan
    link, url = pay_mod.create_payment_link(session, session.get(Loan, "L-1"))
    session.commit()
    assert link.status == "Pending" and "/pay/" in url

    result = pay_mod.mark_paid(session, link.token)
    assert result["ok"] and result["escalation_stopped"] is True

    session.refresh(link); session.refresh(case)
    assert link.status == "Paid"
    assert case.status == "ReviewToClose"
    # a 'Payment received' action was recorded
    assert session.query(RecoveryAction).filter_by(loan_no="L-1", type="Payment received").count() == 1


def test_payment_callback_is_idempotent(session, make_loan):
    from app.models import Loan
    make_loan("L-1")
    link, _ = pay_mod.create_payment_link(session, session.get(Loan, "L-1"))
    session.commit()
    r1 = pay_mod.mark_paid(session, link.token)
    r2 = pay_mod.mark_paid(session, link.token)
    assert r1["ok"] and r2.get("already_paid") is True
    assert session.query(RecoveryAction).count() == 1


# --------------------------------------------------------------------------- #
# Milestone 6 — CRB recommend-only
# --------------------------------------------------------------------------- #
def test_flag_for_crb_creates_pending_task_not_submission(session, make_loan, make_rule):
    make_loan("L-1", due_date=d(120))  # Doubtful
    make_rule(action="FlagForCRB", channel="InApp",
              condition={"tier": "Doubtful"}, requires_approval=True)
    run_daily(session, run_date=TODAY)

    tasks = session.query(ApprovalTask).all()
    assert len(tasks) == 1 and tasks[0].status == "Pending"
    # nothing reached CRB-submitted
    assert session.query(CrbSubmission).count() == 0
    ev = session.query(AutomationEvent).filter_by(action="FlagForCRB").first()
    assert ev.status == "Scheduled" and "Pending manager approval" in ev.reason


def test_crb_approval_creates_submission_attributed(session, make_loan, make_rule):
    make_loan("L-1", due_date=d(120))
    make_rule(action="FlagForCRB", channel="InApp",
              condition={"tier": "Doubtful"}, requires_approval=True)
    run_daily(session, run_date=TODAY)
    task = session.query(ApprovalTask).first()

    result = crb_mod.decide(session, task.id, approve=True, decided_by="mgr@bank")
    assert result["status"] == "Approved"
    sub = session.query(CrbSubmission).first()
    assert sub is not None and sub.approved_by == "mgr@bank"
    ev = session.query(AutomationEvent).filter_by(source="crb", status="Sent").first()
    assert ev is not None and "approved by mgr@bank" in ev.reason


def test_crb_reject_logs_reason_no_submission(session, make_loan, make_rule):
    make_loan("L-1", due_date=d(120))
    make_rule(action="FlagForCRB", channel="InApp",
              condition={"tier": "Doubtful"}, requires_approval=True)
    run_daily(session, run_date=TODAY)
    task = session.query(ApprovalTask).first()

    result = crb_mod.decide(session, task.id, approve=False, decided_by="mgr", reason="in a plan")
    assert result["status"] == "Rejected"
    assert session.query(CrbSubmission).count() == 0
    ev = session.query(AutomationEvent).filter_by(source="crb").first()
    assert ev.status == "Cancelled" and "in a plan" in ev.reason


def test_crb_task_deduped_across_runs(session, make_loan, make_rule):
    make_loan("L-1", due_date=d(120))
    make_rule(action="FlagForCRB", channel="InApp",
              condition={"tier": "Doubtful"}, requires_approval=True, cooldown_days=0)
    run_daily(session, run_date=TODAY)
    run_daily(session, run_date=TODAY + timedelta(days=1))
    assert session.query(ApprovalTask).filter_by(status="Pending").count() == 1


# --------------------------------------------------------------------------- #
# Milestone 7 — dashboard
# --------------------------------------------------------------------------- #
def test_dashboard_summary_shape_and_kpi(session, make_loan, make_rule):
    make_loan("L-1")
    make_loan("L-paused", automation_paused=True)
    make_rule(action="SendReminder", channel="SMS", condition={"tier": "Curable"})
    run_daily(session, run_date=TODAY)

    summ = dash_mod.summary(session, today=TODAY, minutes_per_item=5)
    assert summ["rules"][0]["firedCount"] >= 1
    assert any(p["loanNo"] == "L-paused" for p in summ["pausedLoans"])
    assert summ["kpi"]["isEstimate"] is True
    assert summ["kpi"]["minutesPerItem"] == 5
    assert summ["kpi"]["hours"] == round(summ["kpi"]["itemsAutomated"] * 5 / 60.0, 1)


def test_event_log_search(session, make_loan, make_rule):
    make_loan("L-777")
    make_rule(action="SendReminder", channel="SMS", condition={"tier": "Curable"})
    run_daily(session, run_date=TODAY)
    hits = dash_mod.event_log(session, q="L-777")
    assert hits and all(h["loanNo"] == "L-777" for h in hits)
