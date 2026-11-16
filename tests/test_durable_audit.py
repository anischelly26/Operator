import json
import sys

import pytest

from agent.checkpoint import HumanCheckpoint
from agent.models import ToolCall
from agent.tools import ToolRegistry
from sandbox.environment import SandboxEnvironment


def refund():
    return ToolCall(name="execute_action", arguments={
        "ticket_id": "TKT-101", "action_type": "ISSUE_REFUND",
        "payload": {"order_id": "ORD-501", "amount": 25.0}, "idempotency_key": "audited-refund",
    })


def test_approval_and_committed_action_survive_database_reopen(tmp_path):
    path = tmp_path / "audit.db"
    with SandboxEnvironment(db_path=path) as env:
        assert not ToolRegistry(env, lambda **kw: (True, "Verified receipt")).execute(refund()).is_error
    with SandboxEnvironment(db_path=path) as env:
        history = env.audit_ticket("TKT-101")
        assert [e["event_type"] for e in history["events"]] == ["APPROVED", "EXECUTED"]
        assert history["events"][0]["reason"] == "Verified receipt"
        assert history["events"][0]["payload"]["order_id"] == "ORD-501"
        assert len(history["committed_actions"]) == 1


def test_rejected_decision_is_persisted_without_execution():
    with SandboxEnvironment() as env:
        result = ToolRegistry(env, lambda **kw: (False, "Wrong item")).execute(refund())
        assert result.error_type == "UserRejectionError"
        history = env.audit_ticket("TKT-101")
        assert history["events"][0]["event_type"] == "DENIED"
        assert history["events"][0]["reason"] == "Wrong item"
        assert not history["committed_actions"]


def test_broken_checkpoint_is_denied_and_audited():
    with SandboxEnvironment() as env:
        def broken(**kwargs):
            raise RuntimeError("Unavailable")
        assert ToolRegistry(env, broken).execute(refund()).error_type == "UserRejectionError"
        assert env.audit_ticket("TKT-101")["events"][0]["event_type"] == "DENIED"
        assert not env.audit_ticket("TKT-101")["committed_actions"]


def test_unwritable_approval_audit_prevents_financial_write_and_recovers():
    with SandboxEnvironment() as env:
        conn = env.get_connection()
        conn.execute("CREATE TRIGGER deny_audit BEFORE INSERT ON audit_events "
                     "BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END")
        conn.commit()
        tools = ToolRegistry(env, lambda **kw: (True, "Reviewed"))
        assert tools.execute(refund()).error_type == "ToolDownError"
        assert conn.execute("SELECT COUNT(*) FROM refund_ledger").fetchone()[0] == 0
        assert not conn.in_transaction
        conn.execute("DROP TRIGGER deny_audit")
        conn.commit()
        assert not tools.execute(refund()).is_error


def test_status_approval_and_execution_have_separate_audit_events():
    with SandboxEnvironment() as env:
        tools = ToolRegistry(env, lambda **kw: (True, "Reviewed status"))
        result = tools.execute(ToolCall(name="update_ticket_status", arguments={
            "ticket_id": "TKT-101", "status": "ESCALATED", "tags": ["review"],
        }))
        assert not result.is_error
        assert [e["event_type"] for e in env.audit_ticket("TKT-101")["events"]] == ["APPROVED", "EXECUTED"]


def test_audit_cli_reads_history_without_constructing_live_client(tmp_path, monkeypatch, capsys):
    import main
    path = tmp_path / "cli.db"
    with SandboxEnvironment(db_path=path) as env:
        ToolRegistry(env, lambda **kw: (False, "Need invoice")).execute(refund())
    monkeypatch.setattr(main, "OpenAILLMClient", lambda: pytest.fail("Audit attempted live model access"))
    monkeypatch.setattr(sys, "argv", ["main.py", "--audit", "TKT-101", "--db", str(path)])
    assert main.main() == 0
    history = json.loads(capsys.readouterr().out)
    assert history["ticket_id"] == "TKT-101"
    assert history["events"][0]["reason"] == "Need invoice"


def test_audit_is_ticket_scoped_and_queries_are_parameterized():
    with SandboxEnvironment() as env:
        ToolRegistry(env).execute(refund())
        assert not env.audit_ticket("TKT-102")["events"]
        assert not env.audit_ticket("' OR 1=1 --")["events"]


def test_explicit_sandbox_reset_clears_runtime_audit_and_refund_ledger():
    with SandboxEnvironment() as env:
        ToolRegistry(env, lambda **kw: (True, "Reviewed")).execute(refund())
        env.reset()
        assert env.audit_ticket("TKT-101") == {"ticket_id": "TKT-101", "events": [], "committed_actions": []}
        assert env.get_connection().execute("SELECT COUNT(*) FROM refund_ledger").fetchone()[0] == 0


@pytest.mark.parametrize("options", [{"mode": "invalid"}, {"auto_approve": "false"}])
def test_invalid_checkpoint_configuration_cannot_change_approval_behavior(options):
    with pytest.raises(ValueError):
        HumanCheckpoint(**options)
