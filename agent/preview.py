"""Preview a complete offline workflow against a disposable database copy."""

from pathlib import Path
from contextlib import closing
import sqlite3
import tempfile

from agent.checkpoint import HumanCheckpoint
from agent.llm import MockDeterministicLLMClient
from agent.loop import AgentLoop
from agent.tools import ToolRegistry
import sandbox.environment as environment


def preview_ticket(ticket_id, db_path=None):
    source_path = Path(db_path) if db_path is not None else environment.DEFAULT_DB_PATH
    with tempfile.TemporaryDirectory(prefix="operator-preview-") as directory:
        preview_path = Path(directory) / "preview.db"
        if source_path.exists():
            # The source is opened read-only. Schema upgrades happen only in the
            # disposable copy, including when previewing an older database.
            with closing(sqlite3.connect(source_path.resolve().as_uri() + "?mode=ro", uri=True)) as source:
                with closing(sqlite3.connect(preview_path)) as destination:
                    source.backup(destination)
        with environment.SandboxEnvironment(db_path=preview_path) as env:
            conn = env.get_connection()
            before = {
                row["customer_id"]: row["balance"] for row in conn.execute("SELECT customer_id, balance FROM customers")
            }
            old_actions = {row[0] for row in conn.execute("SELECT idempotency_key FROM executed_actions")}
            old_event_id = conn.execute("SELECT COALESCE(MAX(event_id), 0) FROM audit_events").fetchone()[0]
            ticket_before = conn.execute("SELECT status, tags FROM tickets WHERE ticket_id = ?", (ticket_id,)).fetchone()
            checkpoint = HumanCheckpoint(mode="headless", auto_approve=True)
            tools = ToolRegistry(env, checkpoint.request_approval, ticket_scope=ticket_id)
            trajectory = AgentLoop(MockDeterministicLLMClient(), tools).run("preview", f"Triage ticket {ticket_id}.")
            history = env.audit_ticket(ticket_id)
            ticket_after = conn.execute("SELECT status, tags FROM tickets WHERE ticket_id = ?", (ticket_id,)).fetchone()
            balance_changes = [
                {"customer_id": row["customer_id"], "before": before[row["customer_id"]], "after": row["balance"]}
                for row in conn.execute("SELECT customer_id, balance FROM customers")
                if row["balance"] != before[row["customer_id"]]
            ]
            return {
                "mode": "offline_preview",
                "notice": "Actions and approvals below are simulated in a disposable database copy. The source database is unchanged.",
                "ticket_id": ticket_id,
                "ticket_before": dict(ticket_before) if ticket_before else None,
                "ticket_after": dict(ticket_after) if ticket_after else None,
                "proposed_actions": [a for a in history["committed_actions"] if a["idempotency_key"] not in old_actions],
                "simulated_decisions": [e for e in history["events"] if e["event_id"] > old_event_id],
                "balance_changes": balance_changes,
                "trajectory": trajectory.model_dump(),
            }
