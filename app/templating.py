"""Merge-field rendering for MessageTemplate bodies.

Supported fields: {{borrowerName}}, {{loanNo}}, {{outstandingBalance}},
{{dueDate}}, {{paymentLink}}. Rendering is a safe literal replace — no eval,
no arbitrary attribute access — so a template body can never execute code.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

from app.models import Loan

_FIELD_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")


def loan_merge_context(loan: Loan, payment_link: str | None = None) -> dict[str, str]:
    """Build the merge context for a loan."""
    return {
        "borrowerName": loan.borrower_name,
        "loanNo": loan.loan_no,
        "outstandingBalance": f"{loan.outstanding_balance:,.0f}",
        "dueDate": loan.due_date.isoformat() if loan.due_date else "",
        "product": loan.product or "",
        "paymentLink": payment_link or "",
    }


def render(body: str, context: Mapping[str, Any]) -> str:
    """Replace {{field}} tokens from context. Unknown tokens are left visible as
    [field] so a missing merge value is obvious in preview rather than silent."""
    def _sub(m: re.Match) -> str:
        key = m.group(1)
        if key in context and context[key] not in (None, ""):
            return str(context[key])
        if key in context:
            return ""  # known but empty
        return f"[{key}]"

    return _FIELD_RE.sub(_sub, body)


def merge_fields(body: str) -> list[str]:
    """List the merge fields referenced by a template body (for the editor)."""
    return sorted({m.group(1) for m in _FIELD_RE.finditer(body)})
