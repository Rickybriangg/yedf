# RecoverIQ — Automation Layer

**▶ Live demo:** https://rickybriangg.github.io/yedf/ — a faithful browser port of
the Milestone 1 engine (re-tiering + rule matching + `run_daily`), running with
no server. The real Python API below runs the same logic locally.

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
| **1** | **Rule engine + nightly re-tiering job** | ✅ **this delivery** |
| 2 | Reminder scheduler (SMS/USSD, stubbed gateway) | ⏳ next |
| 3 | Auto-escalation state machine | ⏳ |
| 4 | Templated letter/SMS batch generation | ⏳ |
| 5 | Payment-link / M-Pesa STK push (stubbed) | ⏳ |
| 6 | CRB auto-submission trigger (recommend-only) | ⏳ |
| 7 | Automation dashboard | ⏳ |

Build order is one milestone at a time, reviewed after each.

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

## Data model

`app/models.py`: `Loan`, `RecoveryCase` (minimal scaffold) plus the
automation tables `AutomationRule`, `AutomationEvent` (append-only audit),
`MessageTemplate`. `PaymentLink` / `EscalationPath` / `CrbSubmission` arrive
with their milestones.

## Tests

`pytest -q` — 26 tests covering the acceptance-critical logic:

- **rule matching** — tier, day-range, membership, multi-key AND, bad
  field/operator rejection, type-mismatch handling;
- **cooldown** respected across days (and `cooldown=0` allows daily);
- **idempotent re-run** — same-day re-run adds nothing;
- **paused-loan skip** — silent, no event;
- **opt-out enforcement** — `doNotContact` → `Skipped`, and it does *not* block
  non-contact channels;
- **recommend-only** — `requiresApproval` never auto-`Sent`.

## Guardrails

No live SMS / payment / CRB integration is wired. Every external call will sit
behind an interface with a working stub, so this runs fully offline. Real
provider credentials are **not** wired until explicitly requested.
