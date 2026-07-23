"""Provider seams — every external call sits behind an interface with a working
offline stub. No real SMS/payment provider is wired; swap the stub for a real
implementation (Africa's Talking, Daraja/M-Pesa) without touching callers.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy.orm import Session

from app.models import OutboundMessage, PaymentLink


@dataclass
class SendResult:
    ok: bool
    provider_ref: str
    status: str  # "Sent" | "Failed"


# --------------------------------------------------------------------------- #
# SMS / USSD gateway (Milestone 2)
# --------------------------------------------------------------------------- #
class SmsGateway(Protocol):
    """One method to plug in a real provider later. Implementations must isolate
    all provider-specific code behind this interface."""

    name: str

    def send(
        self,
        session: Session,
        *,
        loan_no: str,
        to_addr: str | None,
        channel: str,
        body: str,
        subject: str | None = None,
        template_id: int | None = None,
        event_id: int | None = None,
    ) -> SendResult: ...


class ConsoleSmsGateway:
    """Stub gateway: 'sends' by logging the rendered message to the DB instead
    of contacting a provider. Runs fully offline with no API keys."""

    name = "ConsoleSmsGateway"

    def send(
        self,
        session: Session,
        *,
        loan_no: str,
        to_addr: str | None,
        channel: str,
        body: str,
        subject: str | None = None,
        template_id: int | None = None,
        event_id: int | None = None,
    ) -> SendResult:
        ref = f"CON-{uuid.uuid4().hex[:12]}"
        msg = OutboundMessage(
            loan_no=loan_no,
            event_id=event_id,
            channel=channel,
            to_addr=to_addr,
            subject=subject,
            body=body,
            template_id=template_id,
            provider=self.name,
            status="Sent",
            created_at=datetime.utcnow(),
        )
        session.add(msg)
        return SendResult(ok=True, provider_ref=ref, status="Sent")


# --------------------------------------------------------------------------- #
# Payment provider (Milestone 5)
# --------------------------------------------------------------------------- #
class PaymentProvider(Protocol):
    """Creates a payment request (e.g. an M-Pesa STK push). Stubbed in dev."""

    name: str

    def create_stk_push(
        self, session: Session, *, loan_no: str, amount: float, base_url: str
    ) -> PaymentLink: ...


class StubPaymentProvider:
    """Simulates an STK push: creates a Pending PaymentLink with a token. A
    (stubbed) callback to /api/payments/callback marks it Paid."""

    name = "MpesaSTK"

    def create_stk_push(
        self, session: Session, *, loan_no: str, amount: float, base_url: str = ""
    ) -> PaymentLink:
        token = uuid.uuid4().hex
        link = PaymentLink(
            loan_no=loan_no,
            amount=amount,
            provider=self.name,
            status="Pending",
            token=token,
            external_ref=f"STK-{token[:10].upper()}",
            created_at=datetime.utcnow(),
        )
        session.add(link)
        session.flush()
        return link

    @staticmethod
    def pay_url(link: PaymentLink, base_url: str = "") -> str:
        """The link embedded in messages ({{paymentLink}}). In dev, hitting the
        callback with this token simulates the borrower paying."""
        base = base_url.rstrip("/") if base_url else ""
        return f"{base}/pay/{link.token}"


# Default singletons used by the engine (swap here to go live).
sms_gateway: SmsGateway = ConsoleSmsGateway()
payment_provider: PaymentProvider = StubPaymentProvider()
