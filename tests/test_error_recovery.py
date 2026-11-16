"""
Tests for error taxonomy and recovery behaviors for The Operator.
Validates each error class from errors.md:
1. BadArgumentsError
2. EmptyResultError
3. WrongToolError
4. PolicyViolationError
5. SecurityViolationError
6. UserRejectionError
7. ToolDownError
"""

import pytest
from agent.checkpoint import HumanCheckpoint
from agent.models import ToolCall
from agent.tools import ToolRegistry
from sandbox.environment import SandboxEnvironment


@pytest.fixture
def sandbox():
    env = SandboxEnvironment()
    env.reset()
    yield env
    env.close()


def test_error_class_bad_arguments(sandbox):
    tools = ToolRegistry(sandbox)
    # Missing required 'ticket_id' parameter
    call = ToolCall(name="get_ticket", arguments={})
    res = tools.execute(call)
    assert res.is_error
    assert res.error_type == "BadArgumentsError"
    assert "Bad arguments error for tool 'get_ticket'" in res.output
    assert "ticket_id" in res.output
    # Must NOT contain a Python stack trace
    assert "Traceback" not in res.output


def test_error_class_empty_result(sandbox):
    tools = ToolRegistry(sandbox)
    call = ToolCall(name="get_ticket", arguments={"ticket_id": "TKT-NONEXISTENT"})
    res = tools.execute(call)
    assert res.is_error
    assert res.error_type == "EmptyResultError"
    assert "Ticket 'TKT-NONEXISTENT' not found" in res.output


def test_error_class_wrong_tool(sandbox):
    tools = ToolRegistry(sandbox)
    call = ToolCall(name="fake_tool_name", arguments={"foo": "bar"})
    res = tools.execute(call)
    assert res.is_error
    assert res.error_type == "WrongToolError"
    assert "Unknown tool 'fake_tool_name'" in res.output


def test_error_class_policy_violation(sandbox):
    tools = ToolRegistry(sandbox, checkpoint_handler=lambda **kw: (True, "OK"))
    call = ToolCall(
        name="execute_action",
        arguments={
            "ticket_id": "TKT-101",
            "action_type": "ISSUE_REFUND",
            "payload": {"order_id": "ORD-501", "amount": 500.0},
            "idempotency_key": "IDEMP-POL-01",
        },
    )
    res = tools.execute(call)
    assert res.is_error
    assert res.error_type == "PolicyViolationError"
    assert "exceeds operator limit of $150.00" in res.output


def test_error_class_security_violation(sandbox):
    tools = ToolRegistry(sandbox, checkpoint_handler=lambda **kw: (True, "OK"))
    call = ToolCall(
        name="execute_action",
        arguments={
            "ticket_id": "TKT-101",
            "action_type": "RESET_PASSWORD",
            "payload": {"target_email": "impostor@unknown.com"},
            "idempotency_key": "IDEMP-SEC-01",
        },
    )
    res = tools.execute(call)
    assert res.is_error
    assert res.error_type == "SecurityViolationError"
    assert "Security policy violation" in res.output


def test_error_class_user_rejection(sandbox):
    # Operator denies write at human checkpoint
    cp = HumanCheckpoint(mode="headless", auto_approve=False)
    tools = ToolRegistry(sandbox, checkpoint_handler=cp.request_approval)
    call = ToolCall(
        name="execute_action",
        arguments={
            "ticket_id": "TKT-101",
            "action_type": "CANCEL_ORDER",
            "payload": {"order_id": "ORD-502"},
            "idempotency_key": "IDEMP-REJ-01",
        },
    )
    res = tools.execute(call)
    assert res.is_error
    assert res.error_type == "UserRejectionError"
    assert "Human Checkpoint DENIED" in res.output

