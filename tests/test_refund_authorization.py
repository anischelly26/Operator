"""Order-level financial invariants, independent of planner wording."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from threading import Barrier

import pytest

from agent.models import ToolCall
from agent.refunds import RefundPolicyError, authorize_refund
from agent.tools import ToolRegistry
from sandbox.environment import SandboxEnvironment


@pytest.fixture
def env():
    with SandboxEnvironment() as sandbox:
        sandbox.reset()
        yield sandbox


def refund(amount=25.0, key="refund-a", order="ORD-501", ticket="TKT-101"):
    return ToolCall(name="execute_action", arguments={
        "ticket_id": ticket, "action_type": "ISSUE_REFUND", "idempotency_key": key,
        "payload": {"order_id": order, "amount": amount},
    })


def approve(**kwargs):
    return True, "Reviewed order and amount"


def set_order(env, transform, customer="CUST-001", order="ORD-501"):
    conn = env.get_connection()
    orders = json.loads(conn.execute("SELECT orders FROM customers WHERE customer_id = ?", (customer,)).fetchone()[0])
    transform(next(o for o in orders if o["order_id"] == order))
    conn.execute("UPDATE customers SET orders = ? WHERE customer_id = ?", (json.dumps(orders), customer))
    conn.commit()


def financial_state(env):
    conn = env.get_connection()
    return tuple(
        [tuple(row) for row in conn.execute(query)] for query in [
            "SELECT customer_id, balance FROM customers ORDER BY customer_id",
            "SELECT * FROM refund_ledger ORDER BY idempotency_key",
            "SELECT * FROM executed_actions ORDER BY idempotency_key",
        ]
    )


@pytest.mark.parametrize("order", ["ORD-601", "ORD-MISSING"])
def test_refund_requires_customers_own_order(env, order):
    before = financial_state(env)
    result = ToolRegistry(env, approve).execute(refund(order=order))
    assert result.error_type == "PolicyViolationError"
    assert financial_state(env) == before


def test_missing_order_identifier_is_a_schema_error(env):
    call = refund()
    del call.arguments["payload"]["order_id"]
    assert ToolRegistry(env, approve).execute(call).error_type == "BadArgumentsError"


def test_refund_cannot_exceed_paid_order_amount(env):
    before = financial_state(env)
    result = ToolRegistry(env, approve).execute(refund(121.0))
    assert result.error_type == "PolicyViolationError"
    assert financial_state(env) == before


def test_partial_refunds_accumulate_in_integer_cents(env):
    tools = ToolRegistry(env, approve)
    assert not tools.execute(refund(0.1, key="cents-a")).is_error
    assert not tools.execute(refund(0.2, key="cents-b")).is_error
    conn = env.get_connection()
    assert conn.execute("SELECT SUM(amount_cents) FROM refund_ledger").fetchone()[0] == 30
    assert conn.execute("SELECT balance FROM customers WHERE customer_id = 'CUST-001'").fetchone()[0] == 45.8


def test_fractions_of_a_cent_are_rejected(env):
    assert ToolRegistry(env, approve).execute(refund(1.001)).error_type == "BadArgumentsError"


def test_new_key_cannot_repeat_a_full_refund(env):
    tools = ToolRegistry(env, approve)
    assert not tools.execute(refund(120.0)).is_error
    before = financial_state(env)
    assert tools.execute(refund(120.0, key="new-key")).error_type == "PolicyViolationError"
    assert financial_state(env) == before


def test_same_key_replay_needs_no_new_approval_or_ledger_entry(env):
    tools = ToolRegistry(env, approve)
    assert not tools.execute(refund()).is_error
    before = financial_state(env)
    events_before = env.audit_ticket("TKT-101")["events"]
    tools.checkpoint_handler = None
    assert not tools.execute(refund()).is_error
    assert financial_state(env) == before
    assert env.audit_ticket("TKT-101")["events"] == events_before


def test_refund_ceiling_spans_multiple_tickets(env):
    conn = env.get_connection()
    conn.execute("INSERT INTO tickets (ticket_id, customer_id, sender_email, subject, body) "
                 "VALUES ('TKT-OTHER', 'CUST-001', 'alice@example.com', 'Same order', 'ORD-501')")
    conn.commit()
    tools = ToolRegistry(env, approve)
    assert not tools.execute(refund(80.0)).is_error
    before = financial_state(env)
    assert tools.execute(refund(50.0, key="other-ticket", ticket="TKT-OTHER")).error_type == "PolicyViolationError"
    assert financial_state(env) == before


def test_splitting_requests_cannot_bypass_operator_threshold(env):
    tools = ToolRegistry(env, approve)
    conn = env.get_connection()
    conn.execute("INSERT INTO tickets (ticket_id, customer_id, sender_email, subject, body) "
                 "VALUES ('TKT-LARGE', 'CUST-003', 'charlie@enterprise.org', 'Refund', 'ORD-701')")
    conn.commit()
    assert not tools.execute(refund(150.0, order="ORD-701", ticket="TKT-LARGE")).is_error
    assert tools.execute(refund(1.0, key="split", order="ORD-701", ticket="TKT-LARGE")).error_type == "PolicyViolationError"


@pytest.mark.parametrize("date", [
    None, "not-a-date", "2026-01-01T10:00:00",
    (datetime.now(timezone.utc) - timedelta(days=31)).isoformat(),
    (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
])
def test_invalid_or_out_of_window_purchase_dates_require_review(env, date):
    set_order(env, lambda o: o.update(purchased_at=date))
    before = financial_state(env)
    assert ToolRegistry(env, approve).execute(refund()).error_type == "PolicyViolationError"
    assert financial_state(env) == before


def test_thirty_day_boundary_is_explicit(env):
    now = datetime(2026, 11, 16, tzinfo=timezone.utc)
    set_order(env, lambda o: o.update(purchased_at=(now - timedelta(days=30)).isoformat()))
    customer = env.get_connection().execute("SELECT * FROM customers WHERE customer_id = 'CUST-001'").fetchone()
    payload = {"order_id": "ORD-501", "amount": 25.0}
    assert authorize_refund(env.get_connection(), customer, payload, now=now).amount_cents == 2500
    with pytest.raises(RefundPolicyError):
        authorize_refund(env.get_connection(), customer, payload, now=now + timedelta(microseconds=1))


def test_unlinked_legacy_refunds_block_new_credit_without_resetting_data(env):
    conn = env.get_connection()
    conn.execute("INSERT INTO executed_actions (idempotency_key, ticket_id, action_type, payload) "
                 "VALUES ('old', 'TKT-101', 'ISSUE_REFUND', ?)", (json.dumps({"amount": 25.0}),))
    conn.commit()
    before = financial_state(env)
    result = ToolRegistry(env, approve).execute(refund())
    assert result.error_type == "PolicyViolationError"
    assert "historical" in result.output
    assert financial_state(env) == before


def test_existing_database_upgrade_does_not_invent_purchase_dates(env):
    set_order(env, lambda o: o.pop("purchased_at"))
    before = financial_state(env)
    with SandboxEnvironment(db_path=env.db_path) as reopened:
        assert ToolRegistry(reopened, approve).execute(refund()).error_type == "PolicyViolationError"
    assert financial_state(env) == before


def test_concurrent_different_keys_cannot_over_refund(env):
    barrier = Barrier(2)
    def worker(key):
        with SandboxEnvironment(db_path=env.db_path) as sandbox:
            def checkpoint(**kwargs):
                barrier.wait(timeout=10)
                return True, "Reviewed"
            return ToolRegistry(sandbox, checkpoint).execute(refund(80.0, key=key))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, ["concurrent-a", "concurrent-b"]))
    assert sum(not result.is_error for result in results) == 1
    assert sum(result.error_type == "PolicyViolationError" for result in results) == 1
    assert env.get_connection().execute("SELECT SUM(amount_cents) FROM refund_ledger").fetchone()[0] == 8000
    assert env.get_connection().execute("SELECT balance FROM customers WHERE customer_id = 'CUST-001'").fetchone()[0] == 125.5


def test_account_reassignment_during_approval_blocks_execution(env):
    def reassign(**kwargs):
        env.get_connection().execute("UPDATE tickets SET customer_id = 'CUST-002' WHERE ticket_id = 'TKT-101'")
        env.get_connection().commit()
        return True, "Reviewed original customer"
    before = financial_state(env)
    assert ToolRegistry(env, reassign).execute(refund()).error_type == "SecurityViolationError"
    assert financial_state(env) == before


def test_execution_audit_failure_rolls_back_ledger_action_and_balance(env):
    conn = env.get_connection()
    conn.execute("CREATE TRIGGER audit_failure BEFORE INSERT ON audit_events WHEN NEW.event_type = 'EXECUTED' "
                 "BEGIN SELECT RAISE(ABORT, 'simulated audit failure'); END")
    conn.commit()
    before = financial_state(env)
    assert ToolRegistry(env, approve).execute(refund()).error_type == "ToolDownError"
    assert financial_state(env) == before
    assert [e["event_type"] for e in env.audit_ticket("TKT-101")["events"]] == ["APPROVED"]
