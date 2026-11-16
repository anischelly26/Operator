import pytest

from agent.llm import BaseLLMClient
from agent.loop import AgentLoop
from agent.models import ToolResult
from agent.tools import ToolRegistry
from eval.runner import EvalRunner, grade_tools
from sandbox.environment import SandboxEnvironment


class ExpensiveWrite(BaseLLMClient):
    def complete(self, messages):
        return ('Action: execute_action | {"ticket_id":"TKT-101","action_type":"ISSUE_REFUND",'
                '"payload":{"order_id":"ORD-501","amount":25.0},"idempotency_key":"over-budget"}', 100, 20, 0.5)


def test_over_budget_completion_cannot_execute_write():
    env = SandboxEnvironment()
    try:
        env.reset()
        approvals = []
        tools = ToolRegistry(env, lambda **kw: approvals.append(kw))
        trajectory = AgentLoop(ExpensiveWrite(), tools, max_budget_usd=0.25).run("budget", "Triage")
        assert trajectory.status == "STOPPED_BUDGET"
        assert trajectory.total_cost_usd == 0.5
        assert trajectory.total_steps == 1
        assert not tools.call_logs and not approvals
    finally:
        env.close()


@pytest.mark.parametrize("arguments", ["[]", "null", "42", '"string"', "true"])
def test_non_object_tool_arguments_are_recoverable(arguments):
    env = SandboxEnvironment()
    try:
        loop = AgentLoop(ExpensiveWrite(), ToolRegistry(env))
        assert loop._parse_tool_call("get_ticket | " + arguments) is None
    finally:
        env.close()


def test_repeated_calls_cannot_inflate_precision():
    logs = [ToolResult(tool_name="get_ticket", output="OK") for _ in range(4)]
    grade = grade_tools(["get_ticket", "execute_action"], [], logs)
    assert grade["tool_precision"] == 1.0
    assert grade["tool_recall"] == 0.5
    assert grade["missing_tools"] == ["execute_action"]


def test_failed_tool_does_not_satisfy_coverage():
    grade = grade_tools(["get_ticket"], [], [ToolResult(tool_name="get_ticket", output="Failure", is_error=True)])
    assert grade["tool_precision"] == 0
    assert grade["missing_tools"] == ["get_ticket"]


def test_inquiry_must_not_execute_financial_action():
    grade = grade_tools(["get_ticket"], [], [ToolResult(tool_name="execute_action", output="OK")])
    assert grade["unexpected_write"]


def test_evaluation_does_not_reset_interactive_database(tmp_path):
    env = SandboxEnvironment()
    try:
        env.reset()
        env.get_connection().execute("UPDATE customers SET balance = 999 WHERE customer_id = 'CUST-001'")
        env.get_connection().commit()
        runner = EvalRunner(output_dir=tmp_path / "results")
        assert runner.sandbox.db_path != env.db_path
        runner.run_all()
        assert env.get_connection().execute("SELECT balance FROM customers WHERE customer_id = 'CUST-001'").fetchone()[0] == 999
    finally:
        env.close()


def test_offline_cli_never_loads_credentials_or_constructs_live_client(monkeypatch, capsys):
    import sys
    from types import SimpleNamespace
    import main

    def forbidden(*args, **kwargs):
        raise AssertionError("Offline mode accessed live configuration")
    monkeypatch.setitem(sys.modules, "dotenv", SimpleNamespace(load_dotenv=forbidden))
    monkeypatch.setattr(main, "OpenAILLMClient", forbidden)
    monkeypatch.setattr("builtins.input", lambda _: "n")
    env = SandboxEnvironment()
    env.reset()
    env.close()
    main.run_interactive("TKT-101")
    assert "HUMAN_REJECTED" in capsys.readouterr().out
