"""Lookalike payees, changed details, approved domains, category budgets, hold expiry, emergency stop,
mandate history, the agent passport, the budget forecast and the daily briefing."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from countersign.app import create_app
from countersign.config import load_settings
from countersign.models import PaymentIntent
from countersign.policy import lookalike_of
from countersign.preview import PreviewLLM, PreviewPayPal
from countersign.store import Store

from conftest import run

TRUSTED = ["priya.nair@personal.example.com", "accounts@harborandpine.example.com"]


def pay(**fields):
    base = dict(rail="payout", counterparty_email="priya.nair@personal.example.com", counterparty_name="Priya Nair",
                amount=40, purpose="Caption fixes", source_ref="task-1041")
    return PaymentIntent(**{**base, **fields})


def rules(payment):
    return {c["rule"]: c["status"] for c in payment["checks"]}


@pytest.mark.parametrize("email,imitates", [
    ("priyanair.payments@quick-settle-mail.example.com", TRUSTED[0]),
    ("priya.nair@personal.example.co", TRUSTED[0]),
    ("accounts@harb0randpine.example.com", TRUSTED[1]),
    ("accounts@someone-else.example.com", None),
    ("marco.silva@personal.example.com", None),
])
def test_lookalike_detection(email, imitates):
    assert lookalike_of(email, TRUSTED) == imitates


def test_lookalike_address_is_refused_by_policy(guard):
    payment = run(guard.submit(pay(counterparty_email="priya.nair@personal.example.co")))
    assert payment["status"] == "blocked"
    assert rules(payment)["lookalike_payee"] == "deny"


def test_known_name_at_new_address_is_held(guard):
    payment = run(guard.submit(pay(counterparty_email="p.n.studio@elsewhere.example.net")))
    assert rules(payment)["changed_details"] == "escalate"
    assert payment["status"] == "pending_approval"


def test_approved_domain_counts_as_approved(guard, store):
    mandate = store.get_mandate()
    mandate.allowed_domains = ["@LumenStock.example.com"]
    store.save_mandate(mandate)
    assert store.get_mandate().allowed_domains == ["lumenstock.example.com"]
    payment = run(guard.submit(pay(counterparty_email="sales@lumenstock.example.com", counterparty_name="Lumen Stock")))
    assert rules(payment)["known_payee"] == "pass"


def test_category_budget(guard, store):
    mandate = store.get_mandate()
    mandate.category_budgets = {"Marketing": 100}
    store.save_mandate(mandate)
    first = run(guard.submit(pay(amount=80, category="Marketing", source_ref="task-1041")))
    assert rules(first)["category_budget"] == "pass"
    second = run(guard.submit(pay(amount=30, category="Marketing", source_ref="task-1038")))
    assert second["status"] == "blocked"
    assert rules(second)["category_budget"] == "deny"


def test_unsigned_hold_expires_and_releases_budget(guard, store):
    held = run(guard.submit(pay(amount=450, source_ref="task-1043")))
    assert held["status"] == "pending_approval"
    old = (datetime.now(timezone.utc) - timedelta(hours=49)).isoformat(timespec="milliseconds")
    store._db.execute("UPDATE payments SET created_at = ? WHERE id = ?", (old, held["id"]))
    store._db.commit()
    expired = run(guard.expire_holds())
    assert [p["id"] for p in expired] == [held["id"]]
    after = store.get_payment(held["id"])
    assert after["status"] == "rejected" and after["decision"] == "expired"
    assert store.get_proof(held["id"])["data"]["signatures"][-1]["result"] == "no answer in time"


def test_emergency_stop_cancels_undo_window_and_blocks_signing(guard, store):
    held = run(guard.submit(pay(amount=450, source_ref="task-1043")))
    mandate = store.get_mandate()
    mandate.undo_seconds = 60
    store.save_mandate(mandate)
    scheduled = run(guard.submit(pay(amount=20, counterparty_email="marco.silva@personal.example.com", counterparty_name="Marco Silva", purpose="Launch video edit", source_ref="task-1044")))
    assert scheduled["status"] == "scheduled"
    result = run(guard.freeze_all())
    assert result["cancelled"] == 1 and result["still_waiting"] == 1
    assert store.get_payment(scheduled["id"])["status"] == "rejected"
    assert not store.get_mandate().active
    with pytest.raises(Exception, match="emergency stop"):
        run(guard.approve(held["id"]))
    assert run(guard.submit(pay(amount=10, source_ref="task-1038")))["status"] == "blocked"


@pytest.fixture
def api(tmp_path):
    app = create_app(load_settings(), Store(str(tmp_path / "r4.db")), PreviewPayPal(), PreviewLLM())
    with TestClient(app) as client:
        client.put("/api/mandate", json={**client.get("/api/mandate").json(), "undo_seconds": 0})
        yield client


def test_mandate_history_and_restore(api):
    m = api.get("/api/mandate").json()
    api.put("/api/mandate", json={**m, "per_payment_cap": 300})
    history = api.get("/api/mandate/history").json()
    assert history[0]["mandate"]["per_payment_cap"] == 300
    older = next(v for v in history if v["mandate"]["per_payment_cap"] == 500)
    restored = api.post(f"/api/mandate/history/{older['sha256']}/restore").json()
    assert restored["per_payment_cap"] == 500
    assert api.post("/api/mandate/history/nope/restore").status_code == 404


def test_passport_verifies_and_detects_changes(api):
    signed = api.get("/api/passport").json()
    body = {"passport": signed["passport"], "signature": signed["signature"]}
    assert api.post("/api/passport/verify", json=body).json()["valid"]
    forged = {**body, "passport": {**signed["passport"], "limits": {**signed["passport"]["limits"], "per_payment": "50000.00"}}}
    assert not api.post("/api/passport/verify", json=forged).json()["valid"]
    m = api.get("/api/mandate").json()
    api.put("/api/mandate", json={**m, "per_payment_cap": 400})
    stale = api.post("/api/passport/verify", json=body).json()
    assert not stale["valid"]
    assert not next(c for c in stale["checks"] if c["label"].startswith("Rules"))["ok"]


def test_forecast_and_briefing(api):
    assert api.get("/api/stats").json()["forecast"]["days_left"] is None
    api.post("/api/intents", json={"rail": "payout", "counterparty_email": "priya.nair@personal.example.com",
                                   "amount": 200, "purpose": "Caption fixes", "justification": "Owner asked", "actor": "tester"})
    forecast = api.get("/api/stats").json()["forecast"]
    assert forecast["used"] == 200 and "pace" in forecast["text"]
    brief = api.post("/api/briefing").json()
    assert brief["figures"]["requests"] == 1
    assert brief["summary"]["answer"]


def test_emergency_stop_endpoint(api):
    r = api.post("/api/emergency-stop").json()
    assert r["mandate"]["active"] is False
    assert "emergency stop" in r["mandate"]["paused_reason"]


def test_passport_carries_ap2_elements_and_track_record(api):
    api.post("/api/redteam")
    signed = api.get("/api/passport").json()
    claims = signed["passport"]
    assert claims["payment_methods"] == ["PayPal Payouts", "PayPal Invoicing", "PayPal Checkout"]
    assert claims["natural_language"] and claims["payer"]["via"] == "PayPal"
    record = claims["track_record"]
    assert record["window_days"] == 30 and record["audit_log_intact"] is True
    assert record["blocked"] == 0  # simulated hijacks are drills, not the agent's own record
    import json
    body = json.loads(json.dumps({"passport": claims, "signature": signed["signature"]}))
    assert api.post("/api/passport/verify", json=body).json()["valid"]
