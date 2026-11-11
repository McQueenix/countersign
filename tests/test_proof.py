"""Signed proofs: issued on every final decision, verifiable by anyone, and broken by any edit."""

import copy

import httpx
from conftest import run
from test_api import api, wait_for  # noqa: F401  (fixture reuse)

from countersign.inbox import hijack_intent
from countersign.models import PaymentIntent
from countersign.paypal import PayPalClient
from countersign.proof import Notary, canonical
from test_clients import BASE, PAYMENT, FakePayPal


def intent(settings, **overrides):
    base = dict(rail="payout", counterparty_email=settings.contractor_email, counterparty_name="Priya Nair",
                amount=240, currency="USD", purpose="Homepage illustrations", justification="Approved task", source_ref="task-1041")
    return PaymentIntent(**{**base, **overrides})


def test_sent_payment_gets_a_proof_that_verifies(guard, settings, store):
    payment = run(guard.submit(intent(settings)))
    record = store.get_proof(payment["id"])
    assert record and record["data"]["outcome"] == "executed"
    claims = record["data"]
    assert claims["amount"] == "240.00" and claims["payee"] == settings.contractor_email
    assert claims["paypal"]["api"] == "Payouts" and claims["paypal"]["id"]
    assert len(claims["mandate_sha256"]) == 64
    assert [s["role"] for s in claims["signatures"]] == ["requester", "policy", "ai_review"]
    assert guard.notary.verify(claims, record["signature"])


def test_any_edit_breaks_the_signature(guard, settings, store):
    payment = run(guard.submit(intent(settings)))
    record = store.get_proof(payment["id"])
    forged = copy.deepcopy(record["data"])
    forged["amount"] = "2400.00"
    assert not guard.notary.verify(forged, record["signature"])
    forged = copy.deepcopy(record["data"])
    forged["payee"] = "attacker@example.com"
    assert not guard.notary.verify(forged, record["signature"])


def test_blocked_and_refused_payments_also_get_proofs(guard, settings, store, paypal):
    blocked = run(guard.submit(PaymentIntent(**hijack_intent(settings)), actor="hijacked"))
    proof = store.get_proof(blocked["id"])["data"]
    assert proof["outcome"] == "blocked" and "ai_review" in proof["stopped_by"] and proof["paypal"] == {}
    held = run(guard.submit(intent(settings, amount=300)))
    assert store.get_proof(held["id"]) is None  # no proof while a decision is still pending
    run(guard.reject(held["id"]))
    refused = store.get_proof(held["id"])["data"]
    assert refused["outcome"] == "rejected" and refused["signatures"][-1] == {"role": "owner", "result": "refused"}
    assert paypal.calls == []


def test_signing_key_survives_a_restart(store):
    first = Notary(store, "Studio")
    second = Notary(store, "Studio")
    assert first.public_key == second.public_key and first.key_id == second.key_id
    claims = {"hello": "world"}
    assert second.verify(claims, first.sign(claims))


def test_proof_link_travels_inside_the_paypal_payment():
    fake = FakePayPal()
    paypal = PayPalClient(BASE, "id", "secret", "Kestrel Studio", transport=httpx.MockTransport(fake))
    run(paypal.create_payout({**PAYMENT, "verify_url": "http://localhost:8000/verify/cs_test1"}))
    note = fake.requests[-1][2]["items"][0]["note"]
    assert note.startswith("Homepage illustrations") and note.endswith("Verify: http://localhost:8000/verify/cs_test1")


def test_proof_api_public_key_and_verifier(api):  # noqa: F811
    payment = api.post("/api/intents", json={"rail": "payout", "counterparty_email": "priya.nair@personal.example.com",
                                             "amount": 80, "purpose": "Icons", "source_ref": "task-1041"}).json()
    assert payment["status"] == "executed"
    bundle = api.get(f"/api/proofs/{payment['id']}").json()
    key = api.get("/.well-known/countersign-key.json").json()
    assert key["public_key"] == bundle["public_key"] and key["alg"] == "Ed25519"
    good = api.post("/api/proofs/verify", json={"proof": bundle["proof"], "signature": bundle["signature"]}).json()
    assert good["valid"] and all(c["ok"] for c in good["checks"])
    forged = {**bundle["proof"], "amount": "8000.00"}
    bad = api.post("/api/proofs/verify", json={"proof": forged, "signature": bundle["signature"]}).json()
    assert not bad["valid"] and [c["ok"] for c in bad["checks"]][:2] == [False, False]
    assert "Countersign" in api.get(f"/verify/{payment['id']}").text
    held = api.post("/api/intents", json={"rail": "payout", "counterparty_email": "new.person@example.com", "amount": 30, "purpose": "x"}).json()
    assert api.get(f"/api/proofs/{held['id']}").status_code == 404


def test_canonical_form_is_order_independent():
    assert canonical({"b": 1, "a": [1, {"d": 2, "c": 3}]}) == canonical({"a": [1, {"c": 3, "d": 2}], "b": 1})
