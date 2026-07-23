# RecoverIQ — Automation Layer

**▶ Live demo:** https://rickybriangg.github.io/yedf/ — a faithful browser port of
the Milestone 1 engine (re-tiering + rule matching + `run_daily`), running with
no server. The real Python API below runs the same logic locally.

> Hosting: the demo is the single file [`docs/index.html`](docs/index.html),
> published with GitHub Pages **branch deploy**. To turn it on once:
> **Settings → Pages → Build and deployment → Source: “Deploy from a branch” →
> Branch: `main` / `/docs` → Save.** No build step, no Actions, no keys.

An automated recovery-workflow engine for RecoverIQ. The principle: automation
handles high-volume, low-judgement work (reminders, routing, escalation, letter
generation, payment links, CRB submission) so human officers only spend time on
accounts that need a decision. **Nothing that removes discretion runs
unattended** — write-offs, restructures, CRB listing and legal handoff stay
human-gated with an audit trail.

> **Standalone build.** This repo scaffolds its own minimal `Loan` /
> `RecoveryCase` domain so it runs offline with no external services and no API
> keys. To plug it into the real RecoverIQ system, replace the bodies in
> `app/tiering.py` with the existing tier rules (keep the call sites) and point
> `app/models.py` at the real schema.

## Status

| Milestone | Scope | State |
|---|---|---|
| **1** | Rule engine + nightly re-tiering job | ✅ done |
| **2** | Reminder scheduler (SMS/USSD, stubbed gateway) | ✅ done |
| **3** | Auto-escalation state machine | ✅ done |
| **4** | Templated letter/SMS batch generation | ✅ done |
| **5** | Payment-link / M-Pesa STK push (stubbed) | ✅ done |
| **6** | CRB auto-submission trigger (recommend-only) | ✅ done |
| **7** | Automation dashboard | ✅ done |

All seven milestones are implemented and tested (`pytest` — 43 tests). Every
external call (SMS, payment, CRB) sits behind an interface with a working
offline stub; no provider credentials are wired.

## Quick start

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# run the tests
pytest -q

# load sample data and start the API
python -m app.seed
uvicorn app.main:app --reload
```

Then:

```bash
# run the nightly job (date is optional; overrides "today" for testing/backfill)
curl -X POST "http://127.0.0.1:8000/api/automation/run-daily?date=2026-07-23"

# see the audit log of what fired and why
curl "http://127.0.0.1:8000/api/automation/events"

# loans with their freshly-computed tier
curl "http://127.0.0.1:8000/api/loans"
```

Interactive docs at `http://127.0.0.1:8000/docs`.

## Milestone 1 — what it does

The nightly job (`app/automation.py::run_daily`, exposed at
`POST /api/automation/run-daily`):

1. **Re-tiers** every loan — recomputes `recoveryTier`, `arrearsBucket`,
   `dormancyDays` via `app/tiering.py` (the single source of truth; tier rules
   are **not** duplicated in the engine).
2. **Evaluates** every active `AutomationRule` against every loan that is not
   paused, using a **safe typed matcher** over the rule's JSON condition —
   **no `eval()`** (`app/rule_engine.py`).
3. **Creates an `AutomationEvent`** for each match that respects the rule's
   cooldown, recording the rule id, action, channel, status and a
   human-readable reason.

### Core guarantees

- **Everything is logged, explainable and reversible.** Each event carries the
  rule that caused it and a `reason` string.
- **Idempotent.** Re-running the job on the same data creates **zero** new
  events the second time — enforced in code (a `rule:loan:run-date` trigger key)
  and backed by a `UNIQUE` constraint.
- **Paused loans are skipped silently** (`automationPaused`) — no event row.
- **Opt-out is honoured at send time.** `doNotContact` is checked right before a
  contact-channel action, so a change since rule-match time still counts; the
  action is logged as `Skipped`, not sent.
- **Nothing that removes discretion auto-fires.** A rule with
  `requiresApproval` (e.g. `FlagForCRB`) produces a *pending* recommendation,
  never a live submission. One-click Manager approval lands in Milestone 6.

### Rules are data, not code

Rules live in the `automation_rules` table and are editable without a code
change. A condition is a JSON object; keys are AND-ed:

```jsonc
{ "tier": "Curable", "daysPastDue": { "gte": 1, "lte": 3 } }
{ "tier": "Doubtful", "daysSinceLastAction": { "gte": 21 } }
{ "arrearsBucket": ["1-30", "31-60"] }          // list => membership
```

Supported fact fields: `tier`, `arrearsBucket`, `dormancyDays`, `daysPastDue`,
`daysSinceLastAction`, `product`, `outstandingBalance`, `loanNo`.
Operators: `eq ne gt gte lt lte in nin`. Unknown fields/operators are rejected
loudly; a single broken rule is isolated and does not abort the run.

## Milestones 2–7

**M2 — Reminder scheduler.** `SmsGateway` interface with a `ConsoleSmsGateway`
stub that logs the rendered message to the DB (`OutboundMessage`) instead of
sending. Templates merge `{{borrowerName}}`, `{{loanNo}}`,
`{{outstandingBalance}}`, `{{dueDate}}`, `{{paymentLink}}`. `GET
/api/reminders/preview?date=` is a **read-only dry run** — the exact loans and
exact rendered text that *would* send, before anything sends. `doNotContact` is
checked at send time. `GET /api/reminders/calendar` groups scheduled sends by
tier/product/day.

**M3 — Escalation state machine.** `EscalationPath` rows store ordered stages
per tier with day-offsets (tunable data, not code). The nightly job advances a
`RecoveryCase` to the next stage once the offset elapses **and** no
payment/response is logged since it entered the stage; a payment stops
advancement and flags the case *ReviewToClose*; a case stuck 2× its dwell is
flagged for manager review. `GET /api/cases`.

**M4 — Batch generation.** `POST /api/batch/letters` over a worklist (`tier` or
explicit `loanNos`) produces one merged **PDF per account** (offline, fpdf2) +
a zip, logging an `AutomationEvent` per account. `POST /api/batch/sms` emits a
merged CSV (opt-outs excluded). Template CRUD + live preview at
`/api/templates` and `POST /api/templates/{id}/preview`.

**M5 — Payment links.** `PaymentProvider` interface + `StubPaymentProvider`
simulating an STK push. `{{paymentLink}}` is generated fresh per send.
`POST /api/payments/callback` (or visiting `/pay/{token}` in dev) marks the
`PaymentLink` Paid, auto-creates a `RecoveryAction` "Payment received", and
**stops escalation** on the case. Idempotent — a repeat callback is a no-op.

**M6 — CRB, recommend-only.** A `FlagForCRB` rule (or the Doubtful CRB
escalation stage) creates a **pending `ApprovalTask`**, never a live submission.
A Manager approves/rejects via `POST /api/approvals/{id}/approve|reject`. Only on
approval is a stubbed `CrbSubmission` created and a `Sent` event logged,
attributed to the approver. Rejection logs the reason. Nothing reaches
"submitted" without a human click.

**M7 — Dashboard.** `GET /api/dashboard` returns rules with last-fired counts,
scheduled sends today/this week, success/failure by channel, pending approvals,
paused loans, cases flagged for review, and the KPI *"hours of manual work
automated this week"* = (reminders + letters) × editable `minutesPerItem`,
clearly labelled an estimate. `GET /api/automation/events?q=&status=` is the
searchable audit log. A live client-side version is the GitHub Pages demo.

## Data model

`app/models.py`: `Loan`, `RecoveryCase` (escalation state) plus `AutomationRule`,
`AutomationEvent` (append-only audit), `MessageTemplate`, `OutboundMessage`,
`RecoveryAction`, `PaymentLink`, `EscalationPath`, `ApprovalTask`,
`CrbSubmission`. Provider seams live in `app/gateways.py`.

## Tests

`pytest -q` — 43 tests covering the acceptance-critical logic:

- **rule matching** — tier, day-range, membership, multi-key AND, bad
  field/operator rejection, type-mismatch handling;
- **cooldown** respected across days (and `cooldown=0` allows daily);
- **idempotent re-run** — same-day re-run adds nothing;
- **paused-loan skip** — silent, no event;
- **opt-out enforcement** — `doNotContact` → `Skipped`, honoured in reminders,
  preview and SMS batch;
- **gateway** logs the rendered message instead of sending;
- **escalation** — advances on schedule, a logged payment stops it and flags
  the case, a stalled case is flagged for review;
- **batch** — a PDF + audit event per account; SMS CSV excludes opt-outs;
- **payments** — callback marks Paid, stops escalation, idempotent;
- **CRB recommend-only** — pending task not submission; approve creates an
  attributed submission; reject logs the reason; tasks de-duped across runs;
- **dashboard** — KPI estimate math and searchable event log.

## Guardrails

No live SMS / payment / CRB integration is wired. Every external call will sit
behind an interface with a working stub, so this runs fully offline. Real
provider credentials are **not** wired until explicitly requested.
