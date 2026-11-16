"""Regression tests for model-independent execution boundaries."""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from agent.models import ToolCall
from agent.tools import ToolRegistry
from sandbox.environment import SandboxEnvironment


@pytest.fixture
def env():
    sandbox = SandboxEnvironment()
    sandbox.reset()
    yield sandbox
    sandbox.close()


def action(action_type="ISSUE_REFUND", payload=None, key="safe-action", ticket="TKT-101"):
    return ToolCall(name="execute_action", arguments={
        "ticket_id": ticket, "action_type": action_type,
        "payload": {"order_id": "ORD-501", "amount": 25.0} if payload is None else payload,
        "idempotency_key": key,
    })


def approve(**kwargs):
    return True, "Reviewed"


def snapshot(env):
    conn = env.get_connection()
    return (
        [tuple(r) for r in conn.execute("SELECT customer_id, balance, orders FROM customers ORDER BY customer_id")],
        [tuple(r) for r in conn.execute("SELECT * FROM executed_actions ORDER BY idempotency_key")],
    )


def test_missing_checkpoint_denies_write(env):
    before = snapshot(env)
    result = ToolRegistry(env).execute(action())
    assert result.error_type == "UserRejectionError"
    assert snapshot(env) == before


@pytest.mark.parametrize("payload", [
    {}, {"amount": 0.0}, {"amount": -1.0}, {"amount": float("nan")},
    {"amount": float("inf")}, {"amount": -float("inf")},
    {"amount": True}, {"amount": "25"}, {"amount": 25.0, "override": True},
])
def test_invalid_refund_payload_never_reaches_checkpoint(env, payload):
    before = snapshot(env)
    approvals = []
    result = ToolRegistry(env, lambda **kw: approvals.append(kw)).execute(
        action(payload={"order_id": "ORD-501", **payload}),
    )
    assert result.error_type == "BadArgumentsError"
    assert not approvals
    assert snapshot(env) == before


@pytest.mark.parametrize("call", [
    action("RESET_PASSWORD", {}),
    action("RESET_PASSWORD", {"target_email": ""}),
    action("CANCEL_ORDER", {}),
    action("SEND_EMAIL", {"recipient": "alice@example.com"}),
    action(key="   "),
])
def test_incomplete_actions_rejected(env, call):
    before = snapshot(env)
    result = ToolRegistry(env, approve).execute(call)
    assert result.error_type == "BadArgumentsError"
    assert snapshot(env) == before


@pytest.mark.parametrize("order_id", ["ORD-701", "ORD-501", "ORD-MISSING"])
def test_cancellation_checks_customer_and_status(env, order_id):
    before = snapshot(env)
    result = ToolRegistry(env, approve).execute(action("CANCEL_ORDER", {"order_id": order_id}))
    assert result.error_type == "PolicyViolationError"
    assert snapshot(env) == before


def test_reset_checks_registered_email_not_ticket_sender(env):
    conn = env.get_connection()
    conn.execute("UPDATE tickets SET sender_email = 'attacker@example.net' WHERE ticket_id = 'TKT-101'")
    conn.commit()
    tools = ToolRegistry(env, approve)
    denied = tools.execute(action("RESET_PASSWORD", {"target_email": "attacker@example.net"}))
    assert denied.error_type == "SecurityViolationError"
    accepted = tools.execute(action("RESET_PASSWORD", {"target_email": "alice@example.com"}))
    assert not accepted.is_error


def test_email_dispatch_to_external_recipient_blocked(env):
    result = ToolRegistry(env, approve).execute(action("SEND_EMAIL", {
        "recipient": "external@example.net", "subject": "Hello", "body": "Sensitive account details",
    }))
    assert result.error_type == "SecurityViolationError"


@pytest.mark.parametrize("ticket", ["TKT-MISSING", "TKT-ORPHAN"])
def test_missing_ticket_or_account_cannot_record_success(env, ticket):
    if ticket == "TKT-ORPHAN":
        conn = env.get_connection()
        conn.execute(
            "INSERT INTO tickets (ticket_id, customer_id, sender_email, subject, body) VALUES (?, ?, ?, ?, ?)",
            (ticket, "MISSING", "nobody@example.net", "Test", "Test"),
        )
        conn.commit()
    before = snapshot(env)
    result = ToolRegistry(env, approve).execute(action(ticket=ticket))
    assert result.error_type == "EmptyResultError"
    assert snapshot(env) == before


@pytest.mark.parametrize("changed", [
    action(payload={"order_id": "ORD-501", "amount": 26.0}),
    action(ticket="TKT-102"),
    action("RESET_PASSWORD", {"target_email": "alice@example.com"}),
])
def test_idempotency_key_rejects_different_request(env, changed):
    tools = ToolRegistry(env, approve)
    assert not tools.execute(action()).is_error
    before = snapshot(env)
    assert tools.execute(changed).error_type == "IdempotencyConflictError"
    assert snapshot(env) == before


def test_transaction_rolls_back_action_record_and_balance(env):
    conn = env.get_connection()
    conn.execute("CREATE TRIGGER fail_balance BEFORE UPDATE OF balance ON customers BEGIN SELECT RAISE(ABORT, 'simulated failure'); END")
    conn.commit()
    before = snapshot(env)
    tools = ToolRegistry(env, approve)
    assert tools.execute(action()).error_type == "ToolDownError"
    assert snapshot(env) == before
    conn.execute("DROP TRIGGER fail_balance")
    conn.commit()
    assert not tools.execute(action()).is_error


def test_checkpoint_cannot_mutate_approved_payload(env):
    def mutating_checkpoint(**kwargs):
        kwargs["payload"]["amount"] = 9999.0
        return True, "Reviewed"
    before = env.get_connection().execute("SELECT balance FROM customers WHERE customer_id = 'CUST-001'").fetchone()[0]
    assert not ToolRegistry(env, mutating_checkpoint).execute(action()).is_error
    after = env.get_connection().execute("SELECT balance FROM customers WHERE customer_id = 'CUST-001'").fetchone()[0]
    assert after == before + 25.0


def test_policy_rechecked_after_human_approval(env):
    def ship_during_approval(**kwargs):
        conn = env.get_connection()
        row = conn.execute("SELECT orders FROM customers WHERE customer_id = 'CUST-001'").fetchone()
        orders = json.loads(row["orders"])
        orders[1]["status"] = "SHIPPED"
        conn.execute("UPDATE customers SET orders = ? WHERE customer_id = 'CUST-001'", (json.dumps(orders),))
        conn.commit()
        return True, "Reviewed"
    result = ToolRegistry(env, ship_during_approval).execute(action("CANCEL_ORDER", {"order_id": "ORD-502"}))
    assert result.error_type == "PolicyViolationError"
    assert env.get_connection().execute("SELECT COUNT(*) FROM executed_actions").fetchone()[0] == 0


def test_concurrent_duplicate_refund_is_credited_once(env):
    before = env.get_connection().execute("SELECT balance FROM customers WHERE customer_id = 'CUST-001'").fetchone()[0]
    barrier = Barrier(2)
    def execute():
        worker = SandboxEnvironment(db_path=env.db_path)
        try:
            def checkpoint(**kwargs):
                barrier.wait(timeout=5)
                return True, "Reviewed"
            return ToolRegistry(worker, checkpoint).execute(action())
        finally:
            worker.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: execute(), range(2)))
    assert all(not r.is_error for r in results)
    assert sum("Idempotency Guard" in r.output for r in results) == 1
    conn = env.get_connection()
    assert conn.execute("SELECT COUNT(*) FROM executed_actions").fetchone()[0] == 1
    assert conn.execute("SELECT balance FROM customers WHERE customer_id = 'CUST-001'").fetchone()[0] == before + 25.0
