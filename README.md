# The Operator

**CS496 AI Engineering — Project 3**

Mediterranean Institute of Technology (MedTech), Fall 2026

Instructor: Dr. Abdeldjalil Labed

A customer-support triage sandbox with a deterministic offline planner, an optional OpenAI adapter, validated tools, approval checkpoints and SQLite state.

## Quick start

Requires Python 3.11 or later.

```bash
python -m pip install -r requirements.txt
python main.py --ticket TKT-101
```

The default mode is offline. It does not load dotenv, construct an OpenAI client or make model requests. A new database is seeded automatically. High-impact sandbox actions prompt for approval.

```bash
python main.py --ticket TKT-102
python main.py --reset
python main.py --eval
python -m pytest -q
python main.py --preview --ticket TKT-101
python main.py --audit TKT-101
```

Evaluation uses a temporary database and returns a nonzero exit code if any scenario fails. It does not reset the interactive sandbox.

Select a separate sandbox or export a trajectory:

```bash
python main.py --ticket TKT-101 --db sandbox/data/demo.db --trace logs/triage.json
```

Trace files include ticket content and tool results. Keep them local unless reviewed for sharing.

## Runtime flow

The loop reads one ticket, consults relevant policies and customer records, proposes a tool operation, observes its typed result and reports the outcome. Unsupported enquiries are escalated for review. The simulator never converts an invoice, warranty or address enquiry into a refund.

| Component | Responsibility |
| --- | --- |
| `agent/llm.py` | Offline rule-based planner and optional live adapter |
| `agent/loop.py` | Protocol parsing, accounting, stopping conditions and trajectories |
| `agent/models.py` | Tool argument, payload and result validation |
| `agent/tools.py` | Ticket/account scope, policies, approval and transactional writes |
| `agent/checkpoint.py` | Interactive approval or explicit headless test decisions |
| `agent/refunds.py` | Order ownership, purchase window, cumulative limits and exact cent accounting |
| `agent/preview.py` | Complete offline workflow against a disposable database copy |
| `sandbox/environment.py` | SQLite initialization, atomic reset and seed records |
| `eval/runner.py` | Isolated benchmark and report generation |
| `eval/faults.py` | Read failures injected for recovery checks |
| `eval/outcomes.py` | Independent database outcome assertions |

## Execution safeguards

- Refunds, password resets, cancellations and email action records require an approval handler.
- Action-specific schemas reject missing fields, extra keys, nonfinite amounts and invalid types.
- Reset/email recipients must match the registered account email.
- Cancellations are restricted to the ticket customer's PENDING or PROCESSING orders.
- A single-ticket run scopes ticket and customer tool access to that task.
- Identical idempotency keys replay safely; conflicting reuse returns an error.
- Action records and balance/order changes commit together and roll back together.
- Approval receives a copy of the payload; policy is rechecked after approval.
- Identical draft replies do not create duplicate rows.
- Refunds require an explicit customer-owned order and a valid timezone-aware purchase date within 30 days.
- Integer-cent refund records enforce cumulative limits across tickets and idempotency keys: total operator refunds cannot exceed the paid amount or $150 per order.
- Approval decisions survive restarts; a committed financial action and its execution audit are recorded atomically.
- A completion that exhausts the budget is accounted for but cannot execute its proposed tool.
- Failed writes cannot be followed by an accepted success claim without recovery or safe escalation.

These are sandbox boundaries. They do not replace caller authentication, verified damage evidence or settlement through a real payment provider.

## Preview changes and inspect history

`python main.py --preview --ticket TKT-101` runs the offline planner on a temporary database copy. The JSON output shows proposed actions, ticket state before/after and balance changes. Preview approvals are simulated automatically inside that copy; the source database is opened read-only and remains unchanged. If no source database exists, preview uses temporary seed data without creating a source file.

`python main.py --preview --ticket TKT-101 --trace logs/preview.json` also saves the preview report.

`python main.py --audit TKT-101` prints persisted approval/rejection reasons, exact reviewed payloads and committed action records. An approval alone is not proof of execution: look for the separate EXECUTED event and action record. Explicit sandbox reset clears runtime audit and financial history.

Existing databases receive the new tables without resetting their data. Missing/invalid purchase dates and historical refunds without an order ledger require manual review; the program does not guess dates or financial history. Refund payloads must now include `order_id` as well as `amount`; fractional cents are rejected.

## Offline verification

The regression suite contains **127 tests** covering execution boundaries, order-level refund limits, concurrent requests with different keys, exact cents, purchase dates, durable audits, read-only preview, rollback, workflow intent, task scoping, failure handling and evaluator accuracy. Socket connections are blocked during tests.

The **35-scenario offline benchmark** checks required tool outcomes and database changes: ticket status, action type, refund amount, balance deltas, order state and unrelated-ticket integrity. Recovery cases inject failures or pre-existing writes instead of merely describing them in prompts.

Run the commands to obtain the current results. The JSON report is written to `eval/results/eval_report.json`.

**A passing offline suite does not measure live model reliability.** The planner is deterministic. Token counts and costs are estimates; local latency is not API latency. The existing attack fixtures are limited regression cases, not proof of general prompt-injection resistance.

## Optional live mode

Live mode requires the operator to configure their own OpenAI credentials and explicitly pass `--live`. Never commit credentials.

```bash
python main.py --live --ticket TKT-101
```

Only this mode loads dotenv. There is no paid connection ping and no silent fallback to the simulator if live execution fails. The adapter has a request timeout and disables automatic SDK retries.

No live requests were made while developing these changes. Provider-native structured tool calls and live validation remain future work.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `MAX_STEPS` | `10` | Maximum completed loop iterations |
| `BUDGET_LIMIT_USD` | `0.25` | Accounted completion-cost threshold |
| `OPENAI_API_KEY` | unset | Read only in explicitly requested live mode |

Step and budget settings must be positive and finite. The monetary threshold can be exceeded by the last completion's charge; the loop blocks subsequent actions and calls. A step limit cannot force termination of a blocked dependency or human prompt.

See [IMPROVEMENTS.md](IMPROVEMENTS.md) for the review, implementation changes, remaining limitations and patch application instructions. Earlier reports in `postmortem.md`, `threat_model.md` and `errors.md` describe the original prototype; this README and the current implementation supersede conflicting claims.

