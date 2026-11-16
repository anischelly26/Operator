import json
import pytest
from agent.workspace import import_tickets, ticket_queue, preview_batch
from sandbox.environment import SandboxEnvironment


def incoming(ticket="TKT-NEW", customer="CUST-001"):
    return {"ticket_id": ticket, "customer_id": customer, "sender_email": "alice@example.com",
            "subject": "Refund ORD-501", "body": "Please refund my damaged ORD-501.", "tags": ["billing"]}


def test_import_and_search(tmp_path):
    path = tmp_path / "tickets.json"
    path.write_text(json.dumps([incoming()]), encoding="utf-8")
    with SandboxEnvironment() as env:
        assert import_tickets(env, path)["imported"] == 1
        result = ticket_queue(env, status="OPEN", search="TKT-NEW")
        assert result["returned"] == 1
        assert result["tickets"][0]["tags"] == ["billing"]
        assert env.audit_ticket("TKT-NEW")["events"][0]["event_type"] == "IMPORTED"


@pytest.mark.parametrize("items", [
    [incoming(), incoming()], [incoming(), incoming("TKT-SECOND", "UNKNOWN")],
    [incoming("TKT-101")], [dict(incoming(), status="RESOLVED")],
    [dict(incoming(), body="")], [], {"ticket": "invalid"},
])
def test_invalid_import_is_atomic(tmp_path, items):
    path = tmp_path / "tickets.json"
    path.write_text(json.dumps(items), encoding="utf-8")
    with SandboxEnvironment() as env:
        before = ticket_queue(env)
        with pytest.raises(ValueError):
            import_tickets(env, path)
        assert ticket_queue(env) == before
        assert not env.audit_ticket("TKT-NEW")["events"]


def test_search_is_literal_and_parameterized():
    with SandboxEnvironment() as env:
        assert not ticket_queue(env, search="' OR 1=1 --")["tickets"]
        assert not ticket_queue(env, search="%")["tickets"]
        assert len(ticket_queue(env, limit=1)["tickets"]) == 1


def test_batch_respects_prior_simulated_refunds_and_preserves_source(tmp_path):
    source = tmp_path / "source.db"
    intake = tmp_path / "tickets.json"
    intake.write_text(json.dumps([incoming()]), encoding="utf-8")
    with SandboxEnvironment(db_path=source) as env:
        import_tickets(env, intake)
    before = source.read_bytes()
    report = preview_batch(["TKT-101", "TKT-NEW"], source)
    assert len(report["proposed_actions"]) == 1
    assert report["results"][0]["ticket_status"] == "RESOLVED"
    assert report["results"][1]["ticket_status"] == "ESCALATED"
    assert report["balance_changes"][0]["after"] == 165.5
    assert source.read_bytes() == before


@pytest.mark.parametrize("ids", [[], ["TKT-101", "TKT-101"], ["TKT-MISSING"], ["TKT-" + str(i) for i in range(101)]])
def test_invalid_batch_does_not_create_source(tmp_path, ids):
    source = tmp_path / "absent.db"
    with pytest.raises(ValueError):
        preview_batch(ids, source)
    assert not source.exists()
