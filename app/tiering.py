"""Recovery tiering logic — the single source of truth for a loan's tier,
arrears bucket and dormancy.

In the full RecoverIQ system this logic already exists and must NOT be
duplicated; the automation engine reuses it via these functions. In this
standalone scaffold we define a straightforward days-past-due model. Replace the
bodies (not the call sites) to wire the real system's rules.
"""
from __future__ import annotations

from datetime import date

# Recovery tiers (ordered from healthiest to worst).
PERFORMING = "Performing"
CURABLE = "Curable"
AT_RISK = "At-risk"
DOUBTFUL = "Doubtful"
IMPAIRED = "Impaired"

TIERS = (PERFORMING, CURABLE, AT_RISK, DOUBTFUL, IMPAIRED)


def days_past_due(due_date: date, today: date) -> int:
    """Days a loan is past its due date. Negative/zero => not yet due."""
    return (today - due_date).days


def recovery_tier(dpd: int) -> str:
    if dpd <= 0:
        return PERFORMING
    if dpd <= 30:
        return CURABLE
    if dpd <= 90:
        return AT_RISK
    if dpd <= 180:
        return DOUBTFUL
    return IMPAIRED


def arrears_bucket(dpd: int) -> str:
    if dpd <= 0:
        return "Current"
    if dpd <= 30:
        return "1-30"
    if dpd <= 60:
        return "31-60"
    if dpd <= 90:
        return "61-90"
    if dpd <= 180:
        return "91-180"
    return "180+"


def dormancy_days(last_payment_date: date | None, disbursed_date: date, today: date) -> int:
    """Days since money last moved on the loan (falls back to disbursement)."""
    ref = last_payment_date or disbursed_date
    return (today - ref).days


def days_since_last_action(last_action_date: date | None, disbursed_date: date, today: date) -> int:
    """Days since a recovery action was last logged (falls back to disbursement,
    i.e. 'nothing has ever been done')."""
    ref = last_action_date or disbursed_date
    return (today - ref).days
