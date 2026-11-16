"""Ticket intake, queue inspection and sequential batch simulation."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile

from pydantic import BaseModel, ConfigDict, Field

from agent.checkpoint import HumanCheckpoint
from agent.llm import MockDeterministicLLMClient
from agent.loop import AgentLoop
from agent.tools import ToolRegistry
import sandbox.environment as environment


class IncomingTicket(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)
    ticket_id: str = Field(pattern=r"^TKT-[A-Za-z0-9_-]+$", max_length=100)
    customer_id: str = Field(min_length=1, max_length=100)
    sender_email: str = Field(min_length=3, max_length=254)
    subject: str = Field(min_length=1, max_length=500)
    body: str = Field(min_length=1, max_length=20000)
    tags: list[str] = Field(default_factory=list, max_length=30)


def import_tickets(env, path):
    path = Path(path)
    if path.stat().st_size > 5_000_000:
        raise ValueError("Ticket import exceeds 5 MB")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not 1 <= len(data) <= 1000:
        raise ValueError("Import must contain a JSON array of 1 to 1000 tickets")
    tickets = [IncomingTicket.model_validate(item) for item in data]
    ids = [t.ticket_id for t in tickets]
    if len(set(ids)) != len(ids):
        raise ValueError("Import contains duplicate ticket IDs")
    conn = env.get_connection()
    # Validate the entire batch before writing. Existing IDs are never replaced.
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        for ticket in tickets:
            if conn.execute("SELECT 1 FROM tickets WHERE ticket_id = ?", (ticket.ticket_id,)).fetchone():
                raise ValueError(f"Ticket already exists: {ticket.ticket_id}")
            if not conn.execute("SELECT 1 FROM customers WHERE customer_id = ?", (ticket.customer_id,)).fetchone():
                raise ValueError(f"Unknown customer: {ticket.customer_id}")
        for ticket in tickets:
            conn.execute(
                "INSERT INTO tickets (ticket_id, customer_id, sender_email, subject, body, status, tags) "
                "VALUES (?, ?, ?, ?, ?, 'OPEN', ?)",
                (ticket.ticket_id, ticket.customer_id, ticket.sender_email, ticket.subject, ticket.body, json.dumps(ticket.tags)),
            )
            env.record_audit("IMPORTED", ticket.ticket_id, "IMPORT_TICKET", ticket.ticket_id,
                             {"customer_id": ticket.customer_id}, commit=False)
    return {"imported": len(tickets), "ticket_ids": ids}


def ticket_queue(env, status=None, search="", limit=100):
    if not 1 <= limit <= 1000:
        raise ValueError("Queue limit must be between 1 and 1000")
    if status and status not in {"OPEN", "IN_PROGRESS", "RESOLVED", "ESCALATED", "ARCHIVED"}:
        raise ValueError("Unknown ticket status")
    rows = env.get_connection().execute(
        "SELECT ticket_id, customer_id, subject, status, tags, created_at FROM tickets "
        "WHERE (? IS NULL OR status = ?) AND "
        "(instr(lower(subject), lower(?)) > 0 OR instr(lower(ticket_id), lower(?)) > 0) "
        "ORDER BY created_at, ticket_id LIMIT ?",
        (status, status, search, search, limit),
    ).fetchall()
    result = [dict(row) for row in rows]
    for row in result:
        row["tags"] = json.loads(row["tags"])
    counts = dict(env.get_connection().execute("SELECT status, COUNT(*) FROM tickets GROUP BY status").fetchall())
    return {"counts_by_status": counts, "tickets": result, "returned": len(result)}


def preview_batch(ticket_ids, db_path=None):
    if not ticket_ids or len(ticket_ids) > 100 or len(set(ticket_ids)) != len(ticket_ids):
        raise ValueError("Batch requires 1 to 100 unique ticket IDs")
    source_path = Path(db_path) if db_path else environment.DEFAULT_DB_PATH
    with tempfile.TemporaryDirectory(prefix="operator-batch-") as directory:
        copy_path = Path(directory) / "batch.db"
        if source_path.exists():
            with closing(sqlite3.connect(source_path.resolve().as_uri() + "?mode=ro", uri=True)) as source:
                with closing(sqlite3.connect(copy_path)) as destination:
                    source.backup(destination)
        with environment.SandboxEnvironment(db_path=copy_path) as env:
            conn = env.get_connection()
            for ticket in ticket_ids:
                if not conn.execute("SELECT 1 FROM tickets WHERE ticket_id = ?", (ticket,)).fetchone():
                    raise ValueError(f"Unknown batch ticket: {ticket}")
            old_keys = {r[0] for r in conn.execute("SELECT idempotency_key FROM executed_actions")}
            before = dict(conn.execute("SELECT customer_id, balance FROM customers").fetchall())
            results = []
            # One shared copy: later tickets see earlier simulated refunds.
            for ticket in ticket_ids:
                checkpoint = HumanCheckpoint(mode="headless", auto_approve=True)
                tools = ToolRegistry(env, checkpoint.request_approval, ticket_scope=ticket)
                trajectory = AgentLoop(MockDeterministicLLMClient(), tools).run("batch-" + ticket, f"Triage ticket {ticket}.")
                results.append({"ticket_id": ticket, "trajectory": trajectory.model_dump(),
                                "ticket_status": conn.execute("SELECT status FROM tickets WHERE ticket_id = ?", (ticket,)).fetchone()[0]})
            actions = [dict(r) for r in conn.execute("SELECT * FROM executed_actions") if r["idempotency_key"] not in old_keys]
            for action in actions:
                action["payload"] = json.loads(action["payload"])
            changes = [{"customer_id": r[0], "before": before[r[0]], "after": r[1]}
                       for r in conn.execute("SELECT customer_id, balance FROM customers") if before[r[0]] != r[1]]
            return {"mode": "offline_batch_preview", "source_unchanged": True,
                    "notice": "Sequential simulation in a disposable copy; approvals are simulated.",
                    "results": results, "proposed_actions": actions, "balance_changes": changes}
