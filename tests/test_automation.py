"""Unit tests for the nightly job: re-tiering, matching, cooldown,
idempotency, paused-skip and opt-out enforcement."""
from datetime import timedelta

from app.automation import run_daily
from app.models import AutomationEvent
from app.tiering import CURABLE, DOUBTFUL
from tests.conftest import TODAY, d


def _events(session):
    return session.query(AutomationEvent).all()


def test_retiering_updates_derived_fields(session, make_loan):
    loan = make_loan("L-1", due_date=d(120))  # 120 dpd -> Doubtful
    run_daily(session, run_date=TODAY)
    session.refresh(loan)
    assert loan.recovery_tier == DOUBTFUL
    assert loan.arrears_bucket == "91-180"


def test_matching_rule_creates_event(session, make_loan, make_rule):
    make_loan("L-1")  # 2 dpd -> Curable
    make_rule(condition={"tier": "Curable"})
    summary = run_daily(session, run_date=TODAY)
    assert summary["created"] == 1
    events = _events(session)
    assert len(events) == 1
    assert events[0].status == "Scheduled"
    assert events[0].loan_no == "L-1"
    assert "Matched rule" in events[0].reason


def test_non_matching_rule_creates_nothing(session, make_loan, make_rule):
    make_loan("L-1")  # Curable
    make_rule(condition={"tier": "Doubtful"})
    summary = run_daily(session, run_date=TODAY)
    assert summary["created"] == 0
    assert _events(session) == []


def test_idempotent_same_day_rerun(session, make_loan, make_rule):
    make_loan("L-1")
    make_rule(condition={"tier": "Curable"})
    first = run_daily(session, run_date=TODAY)
    second = run_daily(session, run_date=TODAY)
    assert first["created"] == 1
    assert second["created"] == 0          # no double fire
    assert second["skipped_duplicate"] == 1
    assert len(_events(session)) == 1       # total unchanged


def test_cooldown_respected_across_days(session, make_loan, make_rule):
    make_loan("L-1")
    make_rule(condition={"tier": "Curable"}, cooldown_days=7)

    run_daily(session, run_date=TODAY)                       # fires
    run_daily(session, run_date=TODAY + timedelta(days=3))   # within cooldown -> skip
    assert len(_events(session)) == 1

    third = run_daily(session, run_date=TODAY + timedelta(days=8))  # cooldown elapsed
    assert third["created"] == 1
    assert len(_events(session)) == 2


def test_cooldown_zero_allows_daily(session, make_loan, make_rule):
    make_loan("L-1")
    make_rule(condition={"tier": "Curable"}, cooldown_days=0)
    run_daily(session, run_date=TODAY)
    run_daily(session, run_date=TODAY + timedelta(days=1))
    assert len(_events(session)) == 2


def test_paused_loan_skipped_silently(session, make_loan, make_rule):
    make_loan("L-1", automation_paused=True)
    make_rule(condition={"tier": "Curable"})
    summary = run_daily(session, run_date=TODAY)
    assert summary["created"] == 0
    assert summary["skipped_paused"] == 1
    assert _events(session) == []  # truly silent — no event row


def test_do_not_contact_opt_out_marks_skipped(session, make_loan, make_rule):
    make_loan("L-1", do_not_contact=True)
    make_rule(condition={"tier": "Curable"}, channel="SMS", action="SendReminder")
    summary = run_daily(session, run_date=TODAY)
    events = _events(session)
    assert len(events) == 1
    assert events[0].status == "Skipped"
    assert "doNotContact" in events[0].reason
    assert summary["by_status"].get("Skipped") == 1


def test_opt_out_does_not_block_non_contact_channel(session, make_loan, make_rule):
    # doNotContact is a *contact* opt-out; an InApp notification still fires.
    make_loan("L-1", do_not_contact=True)
    make_rule(condition={"tier": "Curable"}, channel="InApp", action="Notify")
    run_daily(session, run_date=TODAY)
    events = _events(session)
    assert len(events) == 1
    assert events[0].status == "Scheduled"


def test_requires_approval_is_not_auto_fired(session, make_loan, make_rule):
    make_loan("L-1", due_date=d(120))  # Doubtful
    make_rule(
        condition={"tier": "Doubtful"},
        action="FlagForCRB",
        channel="InApp",
        requires_approval=True,
    )
    run_daily(session, run_date=TODAY)
    events = _events(session)
    assert len(events) == 1
    assert events[0].status == "Scheduled"          # pending, never "Sent"
    assert "Pending manager approval" in events[0].reason


def test_broken_rule_isolated_not_fatal(session, make_loan, make_rule):
    make_loan("L-1")
    make_rule(name="good", condition={"tier": "Curable"})
    make_rule(name="broken", condition={"notAField": 1})
    summary = run_daily(session, run_date=TODAY)
    assert summary["created"] == 1                  # good rule still fired
    assert len(summary["rule_errors"]) == 1
    assert summary["rule_errors"][0]["error"]


def test_day_range_rule_matches_only_in_window(session, make_loan, make_rule):
    make_loan("L-early", due_date=d(2))    # 2 dpd, in [1,3]
    make_loan("L-late", due_date=d(10))    # 10 dpd, outside [1,3]
    make_rule(condition={"daysPastDue": {"gte": 1, "lte": 3}})
    run_daily(session, run_date=TODAY)
    events = _events(session)
    assert {e.loan_no for e in events} == {"L-early"}
