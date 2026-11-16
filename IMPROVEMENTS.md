# Implementation review

Branch: `codex/harden-action-safety`

## Changes implemented

| Problem | Result |
| --- | --- |
| Missing checkpoint silently allowed high-impact writes | Missing approval handler denies the action |
| Password reset trusted a ticket's sender | Reset and email recipients must match the registered customer email |
| Cancellations searched every customer | Only the ticket customer's eligible orders can be cancelled |
| Arbitrary dictionaries accepted as action payloads | Action-specific schemas reject missing/extra fields and invalid refund numbers |
| Reused keys hid different operations | Idempotency keys bind to the ticket, action and normalized payload |
| Failed domain update could leave an action record | Action ledger and domain writes commit or roll back together |
| Approval callback could change the payload | Approval receives a deep copy; account policy is rechecked before committing |
| Model could call tools against unrelated records | Single-ticket runs bind tool access to that ticket and its account |
| Repeated draft generation created duplicates | Identical drafts are retained once |
| Budget-crossing completion could still execute a write | Cost is recorded; the proposed operation is blocked |
| Invalid accounting values could bypass limits | Negative/nonfinite costs and malformed token counts fail before tool execution |
| Final-answer parsing accepted ambiguous responses | Protocol headers are anchored; multiple actions and mixed action/final outputs are rejected |
| Failed write followed by a final answer looked successful | Unresolved write errors prevent successful completion unless safely escalated |
| Simulator used conversation keywords and hardcoded values | Decisions use typed observations, ticket content and retrieved account records |
| Customer content could impersonate observation headers | Ticket records and tool-result envelopes use JSON escaping |
| Unknown tickets could fall back to TKT-101 | Missing tickets are reported without processing another customer |
| Enquiries could trigger refunds | Nonfinancial enquiries are acknowledged or escalated without financial writes |
| Evaluation checked status claims alone | Expected tool results and scenario-specific database outcomes are checked |
| Recovery scenarios described errors without producing them | Read errors, duplicate actions and redundant status updates are actually exercised |
| Tests and evaluation reset the user's sandbox | Both use isolated temporary databases |
| New database started empty | A new sandbox is seeded; reset is transactional |
| Offline startup loaded environment credentials | Only explicit live mode loads dotenv; no connection ping or silent live-to-mock fallback |
| No automatic regression gate | Added offline CI for Python 3.11 and 3.13 |

## Verification

Validation uses the offline simulator and local SQLite only. The test suite blocks socket connections. No repository API key was loaded, tested or used.

The benchmark checks ticket states, action type and payload, balances, order mutations and unrelated-ticket changes. Recovery setup is kept outside the planner.

The offline benchmark is a deterministic regression suite, **not evidence of live model quality**. Its costs and token counts are modeled. Expected-tool precision is a coverage proxy rather than a judgment of every reasoning step.

## Remaining work

1. Integrate a real payment provider with verified payment status, supported currencies and settlement reconciliation. Order-linked authorization, purchase dates and cumulative cent-based refund limits are now implemented in the sandbox.
2. Replace the textual ReAct protocol with provider-native structured tool calls and verify that adapter using a separate, explicitly authorized live test environment.
3. Add authenticated user identity and tenant authorization before connecting real customer systems. Task scoping limits this sandbox's tools; it does not authenticate a caller.
4. Add diverse held-out attack cases. Keyword detection and JSON boundaries do not establish general prompt-injection resistance.
5. Add actual fulfillment tools for invoices, warranty, address updates and shipping. Unsupported requests currently escalate rather than pretend to complete.
6. Add provider-side spending controls if a strict monetary cap is required. The local loop cannot undo the charge of the completion that crosses its threshold.

The step limit bounds completed iterations. It is not an unconditional wall-clock termination guarantee when a dependency or human approval blocks.

## Order authorization, durable audit and preview upgrade

- Refunds require a customer-owned order, a valid timezone-aware purchase timestamp within 30 days and a positive whole-cent amount.
- A separate integer-cent ledger enforces paid-amount and cumulative $150 limits per customer/order, across tickets and keys. Both authorization and ledger totals are rechecked under the write lock, preventing concurrent requests from exceeding the limit.
- Ledger entry, account credit, action record and execution audit commit together. Approval decisions persist independently so an approval remains visible even if execution rolls back.
- Account reassignment during approval blocks execution. Status updates reread and merge tags under a write transaction.
- Approval failures deny the action. Missing audit persistence blocks execution instead of producing unaudited financial writes.
- Audit history is queryable with `python main.py --audit TKT-101`. This is a local application log, not a tamper-proof log or authenticated reviewer identity system.
- `python main.py --preview --ticket TKT-101` reports proposed actions and balance/state changes in an offline disposable database copy; the source is read-only. Existing databases and legacy schemas are copied before any preview migrations.
- Existing database contents are preserved during schema upgrades. Unlinked historical refunds or missing purchase dates require manual review rather than an invented migration.

The regression suite now includes 127 tests; the 35-scenario offline benchmark additionally checks the order-linked cent ledger.

## Applying the patch to an existing checkout

Create a branch from the reviewed upstream version, then apply the delivered patch with `git am /path/to/patch.patch`. This preserves upstream files that were not modified, including local configuration. Resolve any conflicts if upstream has changed.
