"""The HTTP API, driven the way the console drives it."""

import time

import pytest
from fastapi.testclient import TestClient

from countersign.app import create_app
from countersign.config import load_settings
from countersign.preview import PreviewLLM, PreviewPayPal
from countersign.store import Store


@pytest.fixture
def api(tmp_path):
    app = create_app(load_settings(), Store(str(tmp_path / "api.db")), PreviewPayPal(), PreviewLLM())
    with TestClient(app) as client:
        yield client


def wait_for(api, predicate, seconds=5):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_console_and_static_files_are_served(api):
    assert "Countersign" in api.get("/").text
    assert api.get("/static/app.js").status_code == 200
    assert "ag-grid-community" in api.get("/").text


def test_first_run_is_seeded(api):
    assert api.get("/api/mandate").json()["per_payment_cap"] == 500
    assert len(api.get("/api/inbox").json()) == 6
    assert api.get("/api/status").json()["chain"]["ok"]


def test_agent_run_approve_and_stats(api):
    assert len(api.post("/api/inbox/run-all").json()["queued"]) == 6
    assert wait_for(api, lambda: all(i["status"] == "done" for i in api.get("/api/inbox").json()))
    payments = {p["source_ref"]: p for p in api.get("/api/payments").json()}
    assert payments["task-1041"]["status"] == "executed"
    assert payments["mail-2207"]["status"] == "blocked"
    held = payments["task-1044"]
    assert held["status"] == "pending_approval"
    approved = api.post(f"/api/payments/{held['id']}/approve", json={"note": "ok"}).json()
    assert approved["status"] == "executed"
    assert api.post(f"/api/payments/{held['id']}/approve", json={}).status_code == 409
    stats = api.get("/api/stats").json()
    assert stats["sent"] == 3 and stats["stopped"] == 1 and stats["week_committed"] == 739.0
    detail = api.get(f"/api/payments/{held['id']}").json()
    assert [a["event"] for a in detail["audit"]][:2] == ["requested", "policy_evaluated"]
    assert api.get("/api/audit/verify").json()["ok"]


def test_hijack_simulation_is_blocked(api):
    payment = api.post("/api/redteam").json()
    assert payment["status"] == "blocked" and payment["paypal"] == {}


def test_outside_agent_intent_and_validation(api):
    bad = api.post("/api/intents", json={"rail": "payout", "counterparty_email": "not-an-email", "amount": 5})
    assert bad.status_code == 422
    held = api.post("/api/intents", json={"rail": "payout", "counterparty_email": "priya.nair@personal.example.com", "amount": 20, "purpose": "Tip", "actor": "crew-bot"}).json()
    assert held["status"] == "pending_approval" and held["actor"] == "crew-bot"


def test_mandate_edit_pause_and_compile(api):
    mandate = api.get("/api/mandate").json()
    mandate["active"] = False
    assert api.put("/api/mandate", json=mandate).json()["active"] is False
    blocked = api.post("/api/intents", json={"rail": "payout", "counterparty_email": "priya.nair@personal.example.com", "amount": 20, "purpose": "x"}).json()
    assert blocked["status"] == "blocked"
    compiled = api.post("/api/mandate/compile", json={"text": "My agent may pay a@b.com up to $90 per payment and $300 per week. Anything over $40 needs my approval."}).json()
    assert (compiled["per_payment_cap"], compiled["auto_approve_limit"], compiled["weekly_budget"], compiled["allowed_payees"]) == (90, 40, 300, ["a@b.com"])
    assert compiled["allow_invoices"] is False and compiled["active"] is True


def test_purchase_round_trip_through_paypal_return(api):
    vendor = "sales@lumenstock.example.com"
    mandate = api.get("/api/mandate").json()
    mandate["allowed_payees"].append(vendor)
    api.put("/api/mandate", json=mandate)
    order = api.post("/api/intents", json={"rail": "purchase", "counterparty_email": vendor, "amount": 49, "purpose": "Stock photo pack", "source_ref": "task-1047"}).json()
    assert order["status"] == "awaiting_payer"
    back = api.get(f"/paypal/return?payment_id={order['id']}&token={order['paypal']['id']}", follow_redirects=False)
    assert back.status_code == 303
    assert api.get(f"/api/payments/{order['id']}").json()["status"] == "executed"


def test_owner_command_and_reset(api):
    api.post("/api/command", json={"text": "Pay Marco Silva <marco.silva@personal.example.com> $120 for the caption fixes"})
    assert wait_for(api, lambda: any(p["amount"] == 120 for p in api.get("/api/payments").json()))
    assert api.post("/api/demo/reset").json()["ok"]
    assert api.get("/api/payments").json() == []
    assert len(api.get("/api/inbox").json()) == 6
