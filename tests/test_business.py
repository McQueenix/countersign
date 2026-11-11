"""Pay when the client pays, invoice reminders, payee confirmation, trust, statements and the cash view."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from countersign.app import create_app
from countersign.config import load_settings
from countersign.guard import GuardError
from countersign.models import PaymentIntent
from countersign.preview import PreviewLLM, PreviewPayPal
from countersign.store import Store

from conftest import run


def invoice(settings):
    return PaymentIntent(rail="invoice", counterparty_email="accounts@harborandpine.example.com", counterparty_name="Harbor & Pine Cafe",
                         amount=1800, purpose="Phase 1 delivered", source_ref="task-1038")


def marco(**fields):
    base = dict(rail="payout", counterparty_email="marco.silva@personal.example.com", counterparty_name="Marco Silva",
                amount=450, purpose="Launch video edit", source_ref="task-1044")
    return PaymentIntent(**{**base, **fields})


def test_pay_when_the_client_pays(guard, settings, paypal):
    bill = run(guard.submit(invoice(settings)))
    assert bill["status"] == "executed"
    held = run(guard.submit(marco()))
    assert held["status"] == "pending_approval"
    waiting = run(guard.approve(held["id"], after_invoice=bill["id"]))
    assert waiting["status"] == "awaiting_client"
    assert ("create_payout", held["id"]) not in paypal.calls
    run(guard.sync(bill["id"]))
    assert guard.store.get_payment(held["id"])["status"] == "awaiting_client"
    paypal.paid_invoices.add(bill["paypal"]["id"])
    run(guard.sync(bill["id"]))
    sent = guard.store.get_payment(held["id"])
    assert sent["status"] == "executed" and ("create_payout", held["id"]) in paypal.calls


def test_pay_when_paid_keeps_its_budget_reserved(guard, settings, store):
    bill = run(guard.submit(invoice(settings)))
    held = run(guard.submit(marco()))
    run(guard.approve(held["id"], after_invoice=bill["id"]))
    assert store.committed_cents(datetime.now(timezone.utc) - timedelta(days=7)) >= 45000


def test_pay_when_paid_waits_while_frozen(guard, settings, paypal):
    bill = run(guard.submit(invoice(settings)))
    held = run(guard.submit(marco()))
    run(guard.approve(held["id"], after_invoice=bill["id"]))
    run(guard.freeze_all())
    paypal.paid_invoices.add(bill["paypal"]["id"])
    run(guard.sync(bill["id"]))
    assert guard.store.get_payment(held["id"])["status"] == "awaiting_client"


def test_pay_when_paid_needs_a_sent_invoice(guard):
    held = run(guard.submit(marco()))
    with pytest.raises(GuardError, match="invoice"):
        run(guard.approve(held["id"], after_invoice="cs_nope"))


def test_reminders_manual_and_automatic(guard, settings, paypal, store):
    bill = run(guard.submit(invoice(settings)))
    reminded = run(guard.remind(bill["id"]))
    assert reminded["paypal"]["reminders"] == 1 and ("remind_invoice", bill["paypal"]["id"]) in paypal.calls
    assert run(guard.remind_due()) == []
    old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat(timespec="milliseconds")
    paypal_state = {**store.get_payment(bill["id"])["paypal"], "last_reminded": old}
    store.update_payment(bill["id"], paypal=paypal_state)
    assert [p["id"] for p in run(guard.remind_due())] == [bill["id"]]
    paypal.paid_invoices.add(bill["paypal"]["id"])
    run(guard.sync(bill["id"]))
    with pytest.raises(GuardError, match="settled"):
        run(guard.remind(bill["id"]))


@pytest.fixture
def api(tmp_path):
    app = create_app(load_settings(), Store(str(tmp_path / "r5.db")), PreviewPayPal(), PreviewLLM())
    with TestClient(app) as client:
        client.put("/api/mandate", json={**client.get("/api/mandate").json(), "undo_seconds": 0})
        yield client


def new_payee(api, email="ana.ruiz@example.com"):
    return api.post("/api/intents", json={"rail": "payout", "counterparty_email": email, "counterparty_name": "Ana Ruiz",
                                         "amount": 60, "purpose": "Logo sketches", "justification": "Owner asked", "actor": "tester"}).json()


def test_payee_confirms_then_owner_can_sign(api):
    held = new_payee(api)
    assert any(c["rule"] == "payee_confirmation" and c["status"] == "escalate" for c in held["checks"])
    assert api.post(f"/api/payments/{held['id']}/approve", json={}).status_code == 409
    link = api.post(f"/api/payments/{held['id']}/confirm-link").json()
    token = link["url"].rsplit("/", 1)[-1]
    assert "ana.ruiz@example.com" in api.get(f"/api/confirmations/{token}").json()["email"]
    assert api.get(f"/confirm/{token}").status_code == 200
    assert api.post(f"/api/confirmations/{token}", json={"action": "confirm"}).json()["ok"]
    assert api.post(f"/api/confirmations/{token}", json={"action": "confirm"}).status_code == 410
    assert api.post(f"/api/payments/{held['id']}/approve", json={}).json()["status"] == "executed"


def test_payee_denies_and_payment_is_refused(api):
    held = new_payee(api, "ana.fake@example.com")
    token = api.post(f"/api/payments/{held['id']}/confirm-link").json()["url"].rsplit("/", 1)[-1]
    api.post(f"/api/confirmations/{token}", json={"action": "deny"})
    assert api.get(f"/api/payments/{held['id']}").json()["status"] == "rejected"


def test_payees_trust_and_statement(api):
    for _ in range(2):
        held = api.post("/api/intents", json={"rail": "payout", "counterparty_email": "priya.nair@personal.example.com", "counterparty_name": "Priya Nair",
                                              "amount": 20 + _, "purpose": "Caption fixes", "justification": "Owner asked", "actor": "tester"}).json()
        if held["status"] == "pending_approval":
            api.post(f"/api/payments/{held['id']}/approve", json={})
    people = {p["email"]: p for p in api.get("/api/payees").json()}
    priya = people["priya.nair@personal.example.com"]
    assert priya["trust"] == "trusted" and priya["paid_ytd"] == 41
    csv = api.get("/api/payees/priya.nair@personal.example.com/statement")
    assert csv.status_code == 200 and "Total paid to you,41.00" in csv.text
    assert api.get("/api/payees/nobody@example.com/statement").status_code == 404


def test_cash_position(api):
    cash = api.get("/api/stats").json()["cash"]
    assert cash["balance"] == 3000 and cash["after"] == 3000
    new_payee(api)
    assert api.get("/api/stats").json()["cash"]["going_out"] == 60


def test_preview_client_pays_endpoint(tmp_path):
    settings = load_settings()
    object.__setattr__(settings, "offline_preview", True)
    app = create_app(settings, Store(str(tmp_path / "p.db")), PreviewPayPal(), PreviewLLM())
    with TestClient(app) as api:
        _client_pays(api)


def _client_pays(api):
    api.put("/api/mandate", json={**api.get("/api/mandate").json(), "undo_seconds": 0})
    api.post("/api/inbox/run-all")
    import time
    for _ in range(100):
        bills = [p for p in api.get("/api/payments").json() if p["rail"] == "invoice" and p["status"] == "executed"]
        if bills:
            break
        time.sleep(0.05)
    paid = api.post(f"/api/preview/invoices/{bills[0]['id']}/paid").json()
    assert paid["paypal"]["status"] == "PAID"
