"""
Idempotency unit and integration tests for The Operator.
Validates that:
1. Running execute_action with the same idempotency key twice does NOT duplicate actions.
2. Customer balances are not double-refunded.
3. Updating ticket status with identical attributes does not write duplicate updates.
"""

import pytest
from agent.models import ExecuteActionArgs, ToolCall, UpdateStatusArgs
from agent.tools import ToolRegistry
from sandbox.environment import SandboxEnvironment


@pytest.fixture
def sandbox():
    env = SandboxEnvironment()
    env.reset()
    yield env
    env.close()


def test_execute_action_idempotency_prevents_duplicate_refunds(sandbox):
    tools = ToolRegistry(sandbox, checkpoint_handler=lambda **kwargs: (True, "Approved"))

    call = ToolCall(
        name="execute_action",
        arguments={
            "ticket_id": "TKT-101",
            "action_type": "ISSUE_REFUND",
            "payload": {"order_id": "ORD-501", "amount": 50.0, "reason": "Damaged goods"},
            "idempotency_key": "IDEMP-REFUND-TKT-101-001",
        },
    )

    # First call: Should succeed and credit $50 to balance
    res1 = tools.execute(call)
    assert not res1.is_error
    assert "committed in sandbox" in res1.output

    # Inspect customer balance
    conn = sandbox.get_connection()
    c = conn.cursor()
    c.execute("SELECT balance FROM customers WHERE customer_id = 'CUST-001'")
    balance_after_first = c.fetchone()[0]

    # Second call with identical idempotency_key
    res2 = tools.execute(call)
    assert not res2.is_error
    assert "Idempotency Guard" in res2.output
    assert "Duplicate execution blocked safely" in res2.output

    # Verify balance was NOT doubled
    c.execute("SELECT balance FROM customers WHERE customer_id = 'CUST-001'")
    balance_after_second = c.fetchone()[0]
    assert balance_after_second == balance_after_first

    # Verify only ONE row exists in executed_actions table
    c.execute("SELECT COUNT(*) FROM executed_actions WHERE idempotency_key = 'IDEMP-REFUND-TKT-101-001'")
    assert c.fetchone()[0] == 1


def test_update_ticket_status_idempotency(sandbox):
    tools = ToolRegistry(sandbox)

    call = ToolCall(
        name="update_ticket_status",
        arguments={
            "ticket_id": "TKT-101",
            "status": "IN_PROGRESS",
            "tags": ["reviewed", "hardware"],
        },
    )

    # First update: changes state
    res1 = tools.execute(call)
    assert "Success: Ticket 'TKT-101' updated" in res1.output

    # Second update with same values: no-op idempotent notice
    res2 = tools.execute(call)
    assert "Idempotent update" in res2.output
    assert "No database modification performed" in res2.output

