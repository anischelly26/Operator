"""
Tools module for The Operator.
Implements 6 tools (3 read, 3 write) with:
- Strict Pydantic input schemas
- Idempotent writes
- Clear, actionable plain-text errors (no stack traces)
- Comprehensive execution logging (input, output, duration, timestamp)
- Policy enforcement and human checkpoint integration
"""

import copy
import json
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple
from pydantic import ValidationError

from agent.models import (
    ACTION_PAYLOAD_MODELS,
    CheckCustomerArgs,
    DraftReplyArgs,
    ExecuteActionArgs,
    GetTicketArgs,
    SearchKBArgs,
    ToolCall,
    ToolResult,
    UpdateStatusArgs,
)
from sandbox.environment import SandboxEnvironment
from agent.refunds import RefundPolicyError, authorize_refund


class ToolRegistry:
    def __init__(self, sandbox: SandboxEnvironment, checkpoint_handler: Optional[Callable] = None, ticket_scope: Optional[str] = None):
        self.sandbox = sandbox
        self.checkpoint_handler = checkpoint_handler
        self.call_logs: List[ToolResult] = []
        self.ticket_scope = ticket_scope

    def _request_approval(self, ticket_id, action_type, payload, idempotency_key):
        reviewed_payload = copy.deepcopy(payload)
        if self.checkpoint_handler is None:
            approved, reason = False, "No approval handler configured."
        else:
            try:
                approved, reason = self.checkpoint_handler(
                    ticket_id=ticket_id, action_type=action_type,
                    payload=copy.deepcopy(reviewed_payload), idempotency_key=idempotency_key,
                )
                approved = approved is True
                reason = str(reason)
            except Exception as error:
                approved, reason = False, f"Approval handler failed ({type(error).__name__})."
        # Persist before any write; if audit persistence fails, execution fails closed.
        self.sandbox.record_audit(
            "APPROVED" if approved else "DENIED", ticket_id, action_type, idempotency_key,
            reviewed_payload, reason,
        )
        return approved, reason

    def get_schemas(self) -> List[Dict[str, Any]]:
        """Returns OpenAI-compatible function calling schemas."""
        return [
            {
                "type": "function",
                "function": {
                    "name": "get_ticket",
                    "description": "Retrieve ticket details by ID. Returns subject, sender, body, status, and tags.",
                    "parameters": GetTicketArgs.model_json_schema(),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "search_knowledge_base",
                    "description": "Search internal company policies and SOPs (billing, security, shipping, orders).",
                    "parameters": SearchKBArgs.model_json_schema(),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "check_customer_status",
                    "description": "Look up customer account, plan type, balance, and order history.",
                    "parameters": CheckCustomerArgs.model_json_schema(),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "update_ticket_status",
                    "description": "Update the status and category tags of a ticket. Idempotent write.",
                    "parameters": UpdateStatusArgs.model_json_schema(),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "draft_reply",
                    "description": "Save an internal draft response for a ticket. Safe write.",
                    "parameters": DraftReplyArgs.model_json_schema(),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "execute_action",
                    "description": (
                        "Execute high-impact action (SEND_EMAIL, ISSUE_REFUND, RESET_PASSWORD, CANCEL_ORDER). "
                        "Requires an idempotency_key and passes through human checkpoint approval before execution."
                    ),
                    "parameters": ExecuteActionArgs.model_json_schema(),
                },
            },
        ]

    def execute(self, tool_call: ToolCall) -> ToolResult:
        start_time = time.perf_counter()
        name = tool_call.name
        args = tool_call.arguments
        now_iso = datetime.now(timezone.utc).isoformat()

        # Dispatch with error boundary
        try:
            if self.ticket_scope:
                if "ticket_id" in args and args["ticket_id"] != self.ticket_scope:
                    raise PermissionError("Tool call targets a ticket outside the current task.")
                if name == "check_customer_status":
                    account = self.sandbox.get_connection().execute(
                        "SELECT c.customer_id, c.email FROM tickets t JOIN customers c "
                        "ON c.customer_id = t.customer_id WHERE t.ticket_id = ?", (self.ticket_scope,),
                    ).fetchone()
                    if account is None or args.get("customer_id") not in {account["customer_id"], account["email"]}:
                        raise PermissionError("Customer lookup falls outside the current ticket.")
            if name == "get_ticket":
                validated = GetTicketArgs(**args)
                output = self._get_ticket(validated)
                is_error = output.startswith("Error:")
                err_type = "EmptyResultError" if is_error else None

            elif name == "search_knowledge_base":
                validated = SearchKBArgs(**args)
                output = self._search_kb(validated)
                is_error = output.startswith("Error:")
                err_type = "EmptyResultError" if is_error else None

            elif name == "check_customer_status":
                validated = CheckCustomerArgs(**args)
                output = self._check_customer(validated)
                is_error = output.startswith("Error:")
                err_type = "EmptyResultError" if is_error else None

            elif name == "update_ticket_status":
                validated = UpdateStatusArgs(**args)
                output = self._update_ticket_status(validated)
                denied = output.startswith("Human Checkpoint DENIED")
                is_error = output.startswith("Error:") or denied
                err_type = "UserRejectionError" if denied else ("WrongToolError" if is_error else None)

            elif name == "draft_reply":
                validated = DraftReplyArgs(**args)
                output = self._draft_reply(validated)
                is_error = output.startswith("Error:")
                err_type = "WrongToolError" if is_error else None

            elif name == "execute_action":
                validated = ExecuteActionArgs(**args)
                output, is_error, err_type = self._execute_action(validated)

            else:
                output = f"Error: Unknown tool '{name}'. Available tools: get_ticket, search_knowledge_base, check_customer_status, update_ticket_status, draft_reply, execute_action."
                is_error = True
                err_type = "WrongToolError"

        except PermissionError as error:
            output = f"Security policy violation: {error}"
            is_error = True
            err_type = "ScopeViolationError"

        except ValidationError as ve:
            # Format validation error cleanly for LLM consumption
            errors_str = "; ".join([f"{e['loc']}: {e['msg']}" for e in ve.errors()])
            output = f"Bad arguments error for tool '{name}': {errors_str}. Please review schema and correct parameters."
            is_error = True
            err_type = "BadArgumentsError"

        except Exception as e:
            output = f"Execution error in tool '{name}': {str(e)}. Please retry or inspect parameters."
            is_error = True
            err_type = "ToolDownError"

        duration_ms = (time.perf_counter() - start_time) * 1000.0
        result = ToolResult(
            tool_name=name,
            output=output,
            is_error=is_error,
            error_type=err_type,
            duration_ms=round(duration_ms, 2),
            timestamp=now_iso,
        )
        self.call_logs.append(result)
        return result

    # -----------------------------------------------------------------------
    # Tool Implementations
    # -----------------------------------------------------------------------

    def _get_ticket(self, args: GetTicketArgs) -> str:
        conn = self.sandbox.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT ticket_id, customer_id, sender_email, subject, body, status, tags FROM tickets WHERE ticket_id = ?",
            (args.ticket_id,),
        )
        row = cursor.fetchone()
        if not row:
            return f"Error: Ticket '{args.ticket_id}' not found in database. Please verify the ticket ID."

        # JSON escaping prevents customer text from forging observation headers.
        record = dict(row)
        record["tags"] = json.loads(record["tags"])
        record["untrusted_fields"] = ["subject", "body", "sender_email"]
        return json.dumps(record)

    def _search_kb(self, args: SearchKBArgs) -> str:
        conn = self.sandbox.get_connection()
        cursor = conn.cursor()
        q = f"%{args.query.strip().lower()}%"
        cursor.execute(
            "SELECT article_id, category, title, content FROM knowledge_base "
            "WHERE LOWER(category) LIKE ? OR LOWER(title) LIKE ? OR LOWER(content) LIKE ?",
            (q, q, q),
        )
        rows = cursor.fetchall()
        if not rows:
            return f"Error: No knowledge base articles matched query '{args.query}'. Available categories: billing, security, shipping, orders."

        results = []
        for r in rows:
            results.append(f"[{r['article_id']}] {r['title']} (Category: {r['category']}):\n{r['content']}")
        return "\n\n".join(results)

    def _check_customer(self, args: CheckCustomerArgs) -> str:
        conn = self.sandbox.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT customer_id, name, email, plan, balance, orders FROM customers WHERE customer_id = ? OR email = ?",
            (args.customer_id, args.customer_id),
        )
        row = cursor.fetchone()
        if not row:
            return f"Error: Customer record '{args.customer_id}' not found."

        record = dict(row)
        record["orders"] = json.loads(record["orders"])
        return json.dumps(record)

    def _update_ticket_status(self, args: UpdateStatusArgs) -> str:
        conn = self.sandbox.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT status, tags FROM tickets WHERE ticket_id = ?", (args.ticket_id,))
        row = cursor.fetchone()
        if not row:
            return f"Error: Cannot update non-existent ticket '{args.ticket_id}'."

        existing_status = row["status"]
        existing_tags = set(json.loads(row["tags"]))
        new_tags = set(args.tags)

        # Idempotency check: if status matches and all requested tags are already present, no-op
        if existing_status == args.status and new_tags.issubset(existing_tags):
            return (
                f"Idempotent update: Ticket '{args.ticket_id}' is already at status '{args.status}' "
                f"with tags {list(existing_tags)}. No database modification performed."
            )

        if self.checkpoint_handler:
            approved, reason = self._request_approval(
                ticket_id=args.ticket_id,
                action_type=f"UPDATE_STATUS_TO_{args.status}",
                payload={"new_status": args.status, "tags": sorted(new_tags)},
                idempotency_key=f"{args.ticket_id}-STATUS-{args.status}",
            )
            if approved is not True:
                return f"Human Checkpoint DENIED status update for ticket '{args.ticket_id}'. Reason: {reason}."

        with conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute("SELECT status, tags FROM tickets WHERE ticket_id = ?", (args.ticket_id,)).fetchone()
            if current is None:
                return f"Error: Cannot update non-existent ticket '{args.ticket_id}'."
            merged_tags = sorted(set(json.loads(current["tags"])).union(new_tags))
            conn.execute(
                "UPDATE tickets SET status = ?, tags = ?, updated_at = CURRENT_TIMESTAMP WHERE ticket_id = ?",
                (args.status, json.dumps(merged_tags), args.ticket_id),
            )
            self.sandbox.record_audit(
                "EXECUTED", args.ticket_id, f"UPDATE_STATUS_TO_{args.status}", f"{args.ticket_id}-STATUS-{args.status}",
                {"new_status": args.status, "tags": merged_tags}, commit=False,
            )
        return f"Success: Ticket '{args.ticket_id}' updated to status '{args.status}' with tags {merged_tags}."

    def _draft_reply(self, args: DraftReplyArgs) -> str:
        conn = self.sandbox.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT ticket_id FROM tickets WHERE ticket_id = ?", (args.ticket_id,))
        if not cursor.fetchone():
            return f"Error: Cannot draft reply for non-existent ticket '{args.ticket_id}'."

        values = (args.ticket_id, args.recipient, args.subject, args.body)
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT draft_id FROM drafts WHERE ticket_id = ? AND recipient = ? AND subject = ? AND body = ?",
                values,
            ).fetchone()
            if existing:
                return f"Idempotent draft: Existing reply retained for ticket '{args.ticket_id}'."
            conn.execute(
                "INSERT INTO drafts (ticket_id, recipient, subject, body) VALUES (?, ?, ?, ?)", values,
            )
            self.sandbox.record_audit(
                "EXECUTED", args.ticket_id, "DRAFT_REPLY", f"draft-{args.ticket_id}",
                args.model_dump(), commit=False,
            )
        return f"Success: Draft reply saved for ticket '{args.ticket_id}' to recipient '{args.recipient}'."

    def _action_policy(self, args: ExecuteActionArgs, customer) -> Optional[Tuple[str, bool, str]]:
        """Enforce policy against account data, independently of model reasoning."""
        if args.action_type == "ISSUE_REFUND":
            try:
                authorize_refund(self.sandbox.get_connection(), customer, args.payload)
            except RefundPolicyError as error:
                return f"Policy violation: {error}", True, "PolicyViolationError"
        if args.action_type in {"RESET_PASSWORD", "SEND_EMAIL"}:
            key = "target_email" if args.action_type == "RESET_PASSWORD" else "recipient"
            if args.payload[key].casefold() != customer["email"].strip().casefold():
                return (
                    "Security policy violation: Recipient must match the registered account email. Action blocked.",
                    True, "SecurityViolationError",
                )
        if args.action_type == "CANCEL_ORDER":
            order = next(
                (o for o in json.loads(customer["orders"]) if o["order_id"] == args.payload["order_id"]),
                None,
            )
            if order is None:
                return "Policy violation: Order does not belong to the ticket customer.", True, "PolicyViolationError"
            if order["status"] not in {"PENDING", "PROCESSING"}:
                return "Policy violation: Only PENDING or PROCESSING orders can be cancelled.", True, "PolicyViolationError"
        return None

    def _execute_action(self, args: ExecuteActionArgs) -> Tuple[str, bool, Optional[str]]:
        # Action-specific validation also rejects missing fields, extra keys,
        # booleans, numeric strings and non-finite refund amounts.
        payload_model = ACTION_PAYLOAD_MODELS[args.action_type]
        args = args.model_copy(update={"payload": payload_model(**args.payload).model_dump(exclude_none=True)})
        conn = self.sandbox.get_connection()
        serialized_payload = json.dumps(args.payload, sort_keys=True, allow_nan=False)

        def existing_result():
            row = conn.execute(
                "SELECT ticket_id, action_type, payload, executed_at FROM executed_actions WHERE idempotency_key = ?",
                (args.idempotency_key,),
            ).fetchone()
            if row is None:
                return None
            if (row["ticket_id"] != args.ticket_id or row["action_type"] != args.action_type
                    or json.loads(row["payload"]) != args.payload):
                return (
                    "Idempotency conflict: Key already belongs to a different ticket, action or payload.",
                    True, "IdempotencyConflictError",
                )
            return (
                f"Idempotency Guard: Action with key '{args.idempotency_key}' ({row['action_type']}) "
                f"was already executed at {row['executed_at']}. Duplicate execution blocked safely.",
                False, None,
            )

        def account():
            return conn.execute(
                "SELECT c.customer_id, c.email, c.orders FROM tickets t "
                "JOIN customers c ON c.customer_id = t.customer_id WHERE t.ticket_id = ?",
                (args.ticket_id,),
            ).fetchone()

        prior = existing_result()
        if prior is not None:
            return prior
        customer = account()
        if customer is None:
            return "Error: Ticket or registered customer not found.", True, "EmptyResultError"
        approved_customer_id = customer["customer_id"]
        violation = self._action_policy(args, customer)
        if violation is not None:
            return violation

        # No handler means no approval. Never silently bypass the checkpoint.
        approved, reason = self._request_approval(
            ticket_id=args.ticket_id,
            action_type=args.action_type,
            payload=copy.deepcopy(args.payload),
            idempotency_key=args.idempotency_key,
        )
        if approved is not True:
            return (
                f"Human Checkpoint DENIED action '{args.action_type}' for ticket '{args.ticket_id}'. "
                f"Reason given: {reason}. Action aborted.",
                True, "UserRejectionError",
            )

        # Serialize writers and recheck after approval: both the action record and
        # domain mutation commit together, or both roll back on any exception.
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            prior = existing_result()
            if prior is not None:
                return prior
            customer = account()
            if customer is None:
                return "Error: Ticket or registered customer not found.", True, "EmptyResultError"
            if customer["customer_id"] != approved_customer_id:
                return "Security policy violation: Ticket account changed during approval.", True, "SecurityViolationError"
            violation = self._action_policy(args, customer)
            if violation is not None:
                return violation
            authorization = authorize_refund(conn, customer, args.payload) if args.action_type == "ISSUE_REFUND" else None
            conn.execute(
                "INSERT INTO executed_actions (idempotency_key, ticket_id, action_type, payload) VALUES (?, ?, ?, ?)",
                (args.idempotency_key, args.ticket_id, args.action_type, serialized_payload),
            )
            if args.action_type == "CANCEL_ORDER":
                orders = json.loads(customer["orders"])
                for order in orders:
                    if order["order_id"] == args.payload["order_id"]:
                        order["status"] = "CANCELLED"
                conn.execute(
                    "UPDATE customers SET orders = ? WHERE customer_id = ?",
                    (json.dumps(orders), customer["customer_id"]),
                )
            elif args.action_type == "ISSUE_REFUND":
                conn.execute(
                    "INSERT INTO refund_ledger (idempotency_key, customer_id, order_id, amount_cents) VALUES (?, ?, ?, ?)",
                    (args.idempotency_key, authorization.customer_id, authorization.order_id, authorization.amount_cents),
                )
                conn.execute(
                    "UPDATE customers SET balance = ROUND(balance + ?, 2) WHERE customer_id = ?",
                    (authorization.amount_cents / 100, customer["customer_id"]),
                )
            self.sandbox.record_audit(
                "EXECUTED", args.ticket_id, args.action_type, args.idempotency_key, args.payload, commit=False,
            )
        return (
            f"Success: Action '{args.action_type}' committed in sandbox with idempotency_key '{args.idempotency_key}'. "
            f"Details: {args.payload}",
            False, None,
        )

