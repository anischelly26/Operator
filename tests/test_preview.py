import json
import sys
import pytest

from agent.models import ToolCall
from agent.preview import preview_ticket
from agent.tools import ToolRegistry
from sandbox.environment import SandboxEnvironment


def test_preview_proposes_refund_without_mutating_source(tmp_path):
    path = tmp_path / "source.db"
    with SandboxEnvironment(db_path=path):
        pass
    original_bytes = path.read_bytes()
    report = preview_ticket("TKT-101", path)
    assert report["mode"] == "offline_preview"
    assert report["proposed_actions"][0]["payload"] == {
        "order_id": "ORD-501", "amount": 120.0, "reason": "Customer requested refund",
    }
    assert report["balance_changes"] == [{"customer_id": "CUST-001", "before": 45.5, "after": 165.5}]
    assert report["ticket_before"]["status"] == "OPEN"
    assert report["ticket_after"]["status"] == "RESOLVED"
    assert path.read_bytes() == original_bytes


def test_preview_without_existing_database_leaves_no_source_file(tmp_path):
    path = tmp_path / "missing.db"
    report = preview_ticket("TKT-102", path)
    assert report["trajectory"]["status"] == "SUCCESS"
    assert not path.exists()


def test_preview_migrates_only_copy_of_older_database(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    with SandboxEnvironment(db_path=path) as env:
        env.get_connection().execute("DROP TABLE refund_ledger")
        env.get_connection().execute("DROP TABLE audit_events")
        env.get_connection().commit()
    original_bytes = path.read_bytes()
    assert preview_ticket("TKT-101", path)["trajectory"]["status"] == "SUCCESS"
    assert path.read_bytes() == original_bytes
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='refund_ledger'").fetchone() is None


def test_preview_replays_prior_refund_without_proposing_duplicate(tmp_path):
    path = tmp_path / "prior.db"
    with SandboxEnvironment(db_path=path) as env:
        call = ToolCall(name="execute_action", arguments={
            "ticket_id": "TKT-101", "action_type": "ISSUE_REFUND",
            "payload": {"order_id": "ORD-501", "amount": 120.0, "reason": "Customer requested refund"},
            "idempotency_key": "TKT-101-ISSUE_REFUND-001",
        })
        assert not ToolRegistry(env, lambda **kw: (True, "Reviewed")).execute(call).is_error
    report = preview_ticket("TKT-101", path)
    assert not report["proposed_actions"]
    assert not report["balance_changes"]


def test_preview_unknown_ticket_has_no_proposed_changes(tmp_path):
    report = preview_ticket("TKT-NOT-FOUND", tmp_path / "missing.db")
    assert not report["proposed_actions"] and not report["balance_changes"]
    assert report["ticket_before"] is None and report["ticket_after"] is None


def test_preview_cli_exports_report_without_loading_credentials(tmp_path, monkeypatch, capsys):
    import main
    trace = tmp_path / "preview.json"
    source = tmp_path / "source.db"
    monkeypatch.setattr(main, "OpenAILLMClient", lambda: (_ for _ in ()).throw(AssertionError("Live client constructed")))
    monkeypatch.setattr(sys, "argv", ["main.py", "--preview", "--ticket", "TKT-101", "--db", str(source), "--trace", str(trace)])
    assert main.main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "offline_preview"
    assert json.loads(trace.read_text(encoding="utf-8")) == report
    assert not source.exists()


def test_preview_corrupt_database_returns_clean_cli_error(tmp_path, monkeypatch, capsys):
    import main
    source = tmp_path / "corrupt.db"
    source.write_bytes(b"This is not a SQLite database")
    monkeypatch.setattr(sys, "argv", ["main.py", "--preview", "--db", str(source)])
    with pytest.raises(SystemExit) as error:
        main.main()
    assert error.value.code == 2
    assert "file error" in capsys.readouterr().err
    assert source.read_bytes() == b"This is not a SQLite database"
