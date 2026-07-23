"""Test fixtures — an isolated in-memory SQLite DB per test."""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app import models  # noqa: F401  (register mappers)
from app.models import AutomationRule, Loan

TODAY = date(2026, 7, 23)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    s = Session()
    try:
        yield s
    finally:
        s.close()


def d(days_ago: int) -> date:
    """A date `days_ago` before the fixed TODAY."""
    return TODAY - timedelta(days=days_ago)


@pytest.fixture
def make_loan(session):
    """Factory to insert a loan with sensible defaults."""
    def _make(loan_no: str, **overrides) -> Loan:
        defaults = dict(
            borrower_name="Test Borrower",
            borrower_phone="+254700000000",
            product="Personal",
            outstanding_balance=10000.0,
            disbursed_date=d(90),
            due_date=d(2),          # 2 days past due -> Curable
            last_payment_date=d(40),
            last_action_date=d(2),
            automation_paused=False,
            do_not_contact=False,
        )
        defaults.update(overrides)
        loan = Loan(loan_no=loan_no, **defaults)
        session.add(loan)
        session.flush()
        return loan
    return _make


@pytest.fixture
def make_rule(session):
    """Factory to insert an automation rule."""
    def _make(**overrides) -> AutomationRule:
        defaults = dict(
            name="Test rule",
            active=True,
            condition={"tier": "Curable"},
            action="SendReminder",
            channel="SMS",
            template_id=None,
            cooldown_days=0,
            requires_approval=False,
        )
        defaults.update(overrides)
        rule = AutomationRule(**defaults)
        session.add(rule)
        session.flush()
        return rule
    return _make
