"""Milestone 4 — templated letter/SMS batch generation.

"Generate batch" over a worklist produces one PDF per account (demand letters)
or a merged SMS CSV, and logs an AutomationEvent per account so every generated
document is audited. PDFs are written offline with fpdf2 (no external service).
"""
from __future__ import annotations

import csv
import io
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AutomationEvent, Loan, MessageTemplate, OutboundMessage
from app.templating import loan_merge_context, render

try:  # PDF generation is offline via fpdf2; degrade gracefully if missing.
    from fpdf import FPDF

    _HAVE_FPDF = True
except Exception:  # pragma: no cover
    _HAVE_FPDF = False


_DEFAULT_LETTER = (
    "Dear {{borrowerName}},\n\n"
    "RE: DEMAND FOR PAYMENT - LOAN {{loanNo}}\n\n"
    "Our records show that loan {{loanNo}} carries an outstanding balance of "
    "KES {{outstandingBalance}}, which fell due on {{dueDate}} and remains "
    "unpaid.\n\n"
    "We require settlement within seven (7) days of this notice. To pay now, "
    "use {{paymentLink}}. If payment has crossed with this letter, kindly "
    "ignore it.\n\n"
    "Yours faithfully,\nRecoveries Department"
)


def worklist(session: Session, tier: Optional[str] = None) -> list[Loan]:
    stmt = select(Loan).order_by(Loan.loan_no)
    if tier:
        stmt = stmt.where(Loan.recovery_tier == tier)
    return list(session.execute(stmt).scalars())


def _render_letter(loan: Loan, template: Optional[MessageTemplate]) -> tuple[str, str]:
    body_src = template.body if template else _DEFAULT_LETTER
    subject_src = (template.subject if template and template.subject else "Demand for payment — {{loanNo}}")
    ctx = loan_merge_context(loan)
    return render(subject_src, ctx), render(body_src, ctx)


def _latin1(text: str) -> str:
    """The built-in PDF fonts are latin-1 only; fold common unicode punctuation
    to ASCII so demand letters render without a bundled TTF font."""
    replacements = {"—": "-", "–": "-", "‘": "'", "’": "'", "“": '"', "”": '"', "…": "..."}
    for a, b in replacements.items():
        text = text.replace(a, b)
    return text.encode("latin-1", "replace").decode("latin-1")


def _letter_pdf_bytes(subject: str, body: str) -> bytes:
    """Render a single demand letter to PDF bytes."""
    pdf = FPDF(format="A4")
    pdf.set_margins(20, 20, 20)
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_page()
    w = pdf.epw  # effective page width (accounts for margins)
    pdf.set_font("Helvetica", "B", 14)
    pdf.multi_cell(w, 8, _latin1(subject))
    pdf.ln(4)
    pdf.set_font("Helvetica", size=11)
    for para in body.split("\n"):
        pdf.multi_cell(w, 6, _latin1(para) if para else " ")
    out = pdf.output()
    return bytes(out)


def generate_letter_batch(
    session: Session,
    loan_nos: Iterable[str],
    template_id: Optional[int] = None,
    out_dir: Optional[str] = None,
    run_date: Optional[date] = None,
) -> dict[str, Any]:
    """Produce one PDF per account + a zip, logging an event per account."""
    today = run_date or date.today()
    triggered_at = datetime.combine(today, datetime.min.time())
    template = session.get(MessageTemplate, template_id) if template_id else None

    loans = [session.get(Loan, ln) for ln in loan_nos]
    loans = [l for l in loans if l is not None]

    out_path = Path(out_dir) if out_dir else Path.cwd() / "batch_out"
    out_path.mkdir(parents=True, exist_ok=True)
    zip_buf = io.BytesIO()
    manifest: list[dict[str, Any]] = []

    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, loan in enumerate(loans, start=1):
            subject, body = _render_letter(loan, template)
            fname = f"{loan.loan_no}.pdf"
            if _HAVE_FPDF:
                data = _letter_pdf_bytes(subject, body)
            else:  # fallback: plain-text "letter" so the flow still works
                fname = f"{loan.loan_no}.txt"
                data = (subject + "\n\n" + body).encode("utf-8")
            (out_path / fname).write_bytes(data)
            zf.writestr(fname, data)

            event = AutomationEvent(
                loan_no=loan.loan_no, rule_id=None, source="batch",
                triggered_at=triggered_at, action="GenerateLetter", channel="Letter",
                payload={"file": fname, "subject": subject, "template_id": template_id},
                status="Sent",
                reason=f"Demand letter generated in batch ({fname})",
                trigger_key=f"batch:{today.isoformat()}:{loan.loan_no}:{i}",
            )
            session.add(event)
            manifest.append({"loanNo": loan.loan_no, "file": fname, "subject": subject})

    zip_name = f"demand-letters-{today.isoformat()}.zip"
    (out_path / zip_name).write_bytes(zip_buf.getvalue())
    session.commit()

    return {
        "generated": len(manifest),
        "format": "pdf" if _HAVE_FPDF else "txt",
        "zip": str(out_path / zip_name),
        "dir": str(out_path),
        "manifest": manifest,
    }


def generate_sms_csv(
    session: Session,
    loan_nos: Iterable[str],
    template_id: Optional[int] = None,
    out_path: Optional[str] = None,
    run_date: Optional[date] = None,
) -> dict[str, Any]:
    """Produce a merged SMS CSV (loanNo, phone, message), logging per account."""
    today = run_date or date.today()
    triggered_at = datetime.combine(today, datetime.min.time())
    template = session.get(MessageTemplate, template_id) if template_id else None
    body_src = template.body if template else (
        "Hi {{borrowerName}}, loan {{loanNo}} balance KES {{outstandingBalance}} "
        "is overdue (due {{dueDate}}). Please pay to avoid escalation."
    )

    loans = [session.get(Loan, ln) for ln in loan_nos]
    loans = [l for l in loans if l is not None]

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["loanNo", "phone", "message"])
    rows = 0
    for i, loan in enumerate(loans, start=1):
        if loan.do_not_contact:
            continue  # opt-out respected
        msg = render(body_src, loan_merge_context(loan))
        writer.writerow([loan.loan_no, loan.borrower_phone or "", msg])
        rows += 1
        session.add(
            AutomationEvent(
                loan_no=loan.loan_no, rule_id=None, source="batch",
                triggered_at=triggered_at, action="SendReminder", channel="SMS",
                payload={"rendered": msg, "template_id": template_id, "batch": "sms-csv"},
                status="Scheduled",  # queued for the gateway
                reason="SMS queued in batch CSV",
                trigger_key=f"smsbatch:{today.isoformat()}:{loan.loan_no}:{i}",
            )
        )

    csv_text = buf.getvalue()
    path = Path(out_path) if out_path else Path.cwd() / "batch_out" / f"sms-{today.isoformat()}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(csv_text, encoding="utf-8")
    session.commit()
    return {"rows": rows, "csv": str(path), "content": csv_text}
