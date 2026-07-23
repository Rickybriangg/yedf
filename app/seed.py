"""Seed sample data so the engine has something to run against in dev.

Run:  python -m app.seed
Idempotent-ish: it clears the automation-facing tables and reloads them.
"""
from __future__ import annotations

from datetime import date, timedelta

from app.database import SessionLocal, init_db
from app.models import (
    ApprovalTask,
    AutomationEvent,
    AutomationRule,
    CrbSubmission,
    EscalationPath,
    Loan,
    MessageTemplate,
    OutboundMessage,
    PaymentLink,
    RecoveryAction,
    RecoveryCase,
)

TODAY = date(2026, 7, 23)


def _d(days_ago: int) -> date:
    return TODAY - timedelta(days=days_ago)


def seed(session) -> None:
    # Clear (order matters for FKs).
    session.query(CrbSubmission).delete()
    session.query(ApprovalTask).delete()
    session.query(OutboundMessage).delete()
    session.query(PaymentLink).delete()
    session.query(RecoveryAction).delete()
    session.query(AutomationEvent).delete()
    session.query(RecoveryCase).delete()
    session.query(EscalationPath).delete()
    session.query(AutomationRule).delete()
    session.query(MessageTemplate).delete()
    session.query(Loan).delete()
    session.flush()

    # --- Escalation paths (configurable per tier; day-offsets tunable) -------
    session.add_all([
        EscalationPath(tier="Curable", stages=[
            {"label": "SMS reminder", "action": "SendReminder", "channel": "SMS", "offsetDays": 0},
            {"label": "SMS follow-up", "action": "SendReminder", "channel": "SMS", "offsetDays": 3},
        ]),
        EscalationPath(tier="At-risk", stages=[
            {"label": "SMS reminder", "action": "SendReminder", "channel": "SMS", "offsetDays": 0},
            {"label": "Call task", "action": "Call-task", "channel": "InApp", "offsetDays": 3},
            {"label": "Demand letter", "action": "GenerateLetter", "channel": "Letter", "offsetDays": 10},
        ]),
        EscalationPath(tier="Doubtful", stages=[
            {"label": "Demand letter", "action": "GenerateLetter", "channel": "Letter", "offsetDays": 0},
            {"label": "Guarantor contact", "action": "GuarantorContact", "channel": "InApp", "offsetDays": 7},
            {"label": "CRB recommendation", "action": "CRB-recommend", "channel": "InApp", "offsetDays": 21},
        ]),
        EscalationPath(tier="Impaired", stages=[
            {"label": "Legal handoff recommendation", "action": "LegalHandoff-recommend", "channel": "InApp", "offsetDays": 0},
        ]),
    ])

    # --- Templates -----------------------------------------------------------
    t_reminder = MessageTemplate(
        name="Early reminder (SMS)",
        channel="SMS",
        body=(
            "Hi {{borrowerName}}, loan {{loanNo}} has an outstanding balance of "
            "KES {{outstandingBalance}} due {{dueDate}}. Pay via {{paymentLink}}."
        ),
        language="en",
    )
    t_demand = MessageTemplate(
        name="Demand letter",
        channel="Letter",
        subject="Demand for payment — {{loanNo}}",
        body="Dear {{borrowerName}},\n\nOur records show {{loanNo}} is in arrears...",
        language="en",
    )
    session.add_all([t_reminder, t_demand])
    session.flush()

    # --- Rules (configurable data, not hardcoded logic) ----------------------
    rules = [
        AutomationRule(
            name="Curable early reminder",
            active=True,
            condition={"tier": "Curable", "daysPastDue": {"gte": 1, "lte": 3}},
            action="SendReminder",
            channel="SMS",
            template_id=t_reminder.id,
            cooldown_days=3,
        ),
        AutomationRule(
            name="At-risk weekly nudge",
            active=True,
            condition={"tier": "At-risk", "daysSinceLastAction": {"gte": 7}},
            action="SendReminder",
            channel="SMS",
            template_id=t_reminder.id,
            cooldown_days=7,
        ),
        AutomationRule(
            name="Doubtful demand letter",
            active=True,
            condition={"tier": "Doubtful"},
            action="GenerateLetter",
            channel="Letter",
            template_id=t_demand.id,
            cooldown_days=30,
        ),
        AutomationRule(
            name="Doubtful CRB recommendation",
            active=True,
            condition={"tier": "Doubtful", "daysSinceLastAction": {"gte": 21}},
            action="FlagForCRB",
            channel="InApp",
            cooldown_days=90,
            requires_approval=True,  # never auto-fired — Manager approves
        ),
    ]
    session.add_all(rules)

    # --- Loans ---------------------------------------------------------------
    loans = [
        Loan(  # Curable, 2 dpd -> hits early reminder
            loan_no="L-1001",
            borrower_name="Amina Yusuf",
            borrower_phone="+254700000001",
            product="Business",
            outstanding_balance=15000,
            disbursed_date=_d(60),
            due_date=_d(2),
            last_payment_date=_d(35),
            last_action_date=_d(2),
        ),
        Loan(  # At-risk, no action 20d -> weekly nudge
            loan_no="L-1002",
            borrower_name="Brian Otieno",
            borrower_phone="+254700000002",
            product="Personal",
            outstanding_balance=48000,
            disbursed_date=_d(200),
            due_date=_d(45),
            last_payment_date=_d(50),
            last_action_date=_d(20),
        ),
        Loan(  # Doubtful, no action 25d -> letter + CRB recommendation
            loan_no="L-1003",
            borrower_name="Carol Wanjiru",
            borrower_phone="+254700000003",
            product="Business",
            outstanding_balance=120000,
            disbursed_date=_d(400),
            due_date=_d(120),
            last_payment_date=_d(130),
            last_action_date=_d(25),
        ),
        Loan(  # Curable but automation paused -> skipped silently
            loan_no="L-1004",
            borrower_name="Daniel Kimani",
            borrower_phone="+254700000004",
            product="Personal",
            outstanding_balance=8000,
            disbursed_date=_d(40),
            due_date=_d(2),
            last_action_date=_d(2),
            automation_paused=True,
        ),
        Loan(  # Curable but do-not-contact -> reminder Skipped (opt-out)
            loan_no="L-1005",
            borrower_name="Esther Njeri",
            borrower_phone="+254700000005",
            product="Personal",
            outstanding_balance=9500,
            disbursed_date=_d(45),
            due_date=_d(2),
            last_action_date=_d(2),
            do_not_contact=True,
        ),
    ]
    session.add_all(loans)
    session.flush()

    # A recovery case per non-performing loan (used from Milestone 3 onward).
    for ln in loans:
        session.add(RecoveryCase(loan_no=ln.loan_no, status="Open"))

    session.commit()


def main() -> None:
    init_db()
    with SessionLocal() as session:
        seed(session)
        n_loans = session.query(Loan).count()
        n_rules = session.query(AutomationRule).count()
    print(f"Seeded {n_loans} loans and {n_rules} rules (run date {TODAY.isoformat()}).")


if __name__ == "__main__":
    main()
