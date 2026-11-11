"""End-to-end checks of the countersignature pipeline, with PayPal and the model replaced by stand-ins."""

from datetime import date

from conftest import run

from countersign import policy
from countersign.agent import Agent
from countersign.events import EventBus
from countersign.inbox import hijack_intent
from countersign.models import PaymentIntent
from countersign.paypal import PayPalError
from countersign.preview import PreviewLLM


def intent(settings, **overrides):
    base = dict(rail="payout", counterparty_email=settings.contractor_email, counterparty_name="Priya Nair",
                amount=240, currency="USD", purpose="Homepage illustrations", justification="Approved task", source_ref="task-1041")
    return PaymentIntent(**{**base, **overrides})


def statuses(payment):
    return {c["rule"]: c["status"] for c in payment["checks"]}


def test_clean_payout_is_sent(guard, settings, paypal):
    payment = run(guard.submit(intent(settings)))
    assert payment["status"] == "executed"
    assert payment["decision"] == "auto_approved"
    assert payment["paypal"]["api"] == "Payouts"
    assert paypal.calls == [("create_payout", payment["id"])]


def test_over_threshold_waits_for_owner_then_sends(guard, settings, paypal):
    payment = run(guard.submit(intent(settings, counterparty_email=settings.contractor2_email, amount=450, source_ref="task-1044")))
    assert payment["status"] == "pending_approval"
    assert statuses(payment)["auto_approve_limit"] == "escalate"
    assert paypal.calls == []
    approved = run(guard.approve(payment["id"], "ok"))
    assert approved["status"] == "executed" and approved["decided_by"] == "owner"


def test_over_cap_is_blocked_without_touching_paypal_or_the_model(guard, settings, paypal):
    payment = run(guard.submit(intent(settings, amount=2400)))
    assert payment["status"] == "blocked" and payment["decision"] == "blocked_policy"
    assert payment["review"] == {}
    assert paypal.calls == []


def test_hijacked_agent_is_blocked(guard, settings, paypal):
    payment = run(guard.submit(PaymentIntent(**hijack_intent(settings)), actor="hijacked"))
    assert payment["status"] == "blocked" and payment["decision"] == "blocked_ai"
    assert paypal.calls == []


def test_rejected_payment_is_never_sent(guard, settings, paypal):
    payment = run(guard.submit(intent(settings, amount=300)))
    rejected = run(guard.reject(payment["id"], "not now"))
    assert rejected["status"] == "rejected"
    assert paypal.calls == []


def test_same_work_item_cannot_be_paid_twice(guard, settings, paypal):
    run(guard.submit(intent(settings)))
    again = run(guard.submit(intent(settings)))
    assert again["status"] == "blocked"
    assert statuses(again)["duplicate"] == "deny"
    assert len(paypal.calls) == 1


def test_weekly_budget_counts_held_payments(guard, settings, store):
    for n in range(3):
        run(guard.submit(intent(settings, amount=480, source_ref=None, purpose=f"job {n}", counterparty_email=settings.contractor_email if n % 2 else settings.contractor2_email)))
    fourth = run(guard.submit(intent(settings, amount=200, source_ref=None, purpose="job 4")))
    assert fourth["status"] == "blocked"
    assert statuses(fourth)["weekly_budget"] == "deny"


def test_unknown_payee_needs_owner(guard, settings):
    payment = run(guard.submit(intent(settings, counterparty_email="someone.new@example.com", amount=50)))
    assert payment["status"] == "pending_approval"
    assert statuses(payment)["known_payee"] == "escalate"


def test_expired_or_paused_mandate_blocks_everything(store, settings):
    mandate = store.get_mandate()
    checks = policy.evaluate(mandate, intent(settings), store, "record", today=date(2028, 1, 1))
    assert policy.outcome_of(checks) == "deny"
    mandate.active = False
    assert policy.outcome_of(policy.evaluate(mandate, intent(settings), store, "record")) == "deny"
    assert policy.outcome_of(policy.evaluate(None, intent(settings), store, "record")) == "deny"


def test_invoice_and_purchase_rails(guard, settings):
    invoice = run(guard.submit(intent(settings, rail="invoice", counterparty_email=settings.client_email, amount=1800, source_ref="task-1038")))
    assert invoice["status"] == "executed" and invoice["paypal"]["api"] == "Invoicing"
    purchase = run(guard.submit(intent(settings, rail="purchase", counterparty_email=settings.vendor_email, amount=49, source_ref="task-1047")))
    # The vendor is not on the approved list, so the owner signs first; then PayPal asks the owner to pay.
    assert purchase["status"] == "pending_approval"
    order = run(guard.approve(purchase["id"]))
    assert order["status"] == "awaiting_payer"
    captured = run(guard.capture_return(order["id"], order["paypal"]["id"]))
    assert captured["status"] == "executed" and captured["paypal"]["capture_id"]


def test_paypal_error_is_recorded_and_retry_uses_a_new_key(guard, settings, paypal):
    paypal.fail_next = PayPalError(422, "UNPROCESSABLE_ENTITY", "Sender does not have sufficient funds", "debug123")
    failed = run(guard.submit(intent(settings)))
    assert failed["status"] == "failed" and failed["paypal"]["error"]["debug_id"] == "debug123"
    retried = run(guard.retry(failed["id"]))
    assert retried["status"] == "executed"
    assert paypal.calls == [("create_payout", failed["id"] + "-r1")]


def test_unknown_outcome_reuses_the_same_key(guard, settings, paypal):
    paypal.fail_next = PayPalError(0, "NETWORK_ERROR", "Could not reach PayPal")
    failed = run(guard.submit(intent(settings)))
    run(guard.retry(failed["id"]))
    assert paypal.calls == [("create_payout", failed["id"])]


def test_review_failure_holds_for_owner(store, paypal, settings):
    from countersign.guard import Guard
    from countersign.llm import LLMError

    class Broken(PreviewLLM):
        async def json(self, system, user):
            raise LLMError("model offline")

    guard = Guard(store, paypal, Broken(), EventBus(), "http://localhost:8000")
    payment = run(guard.submit(intent(settings)))
    assert payment["status"] == "pending_approval"
    assert paypal.calls == []


def test_audit_chain_detects_tampering(guard, settings, store):
    run(guard.submit(intent(settings)))
    assert store.verify_chain()["ok"]
    store._db.execute("UPDATE audit SET data = replace(data, '240', '9240') WHERE event = 'requested'")
    store._db.commit()
    result = store.verify_chain()
    assert not result["ok"] and result["broken_at"]


def test_agent_works_the_inbox(guard, store, settings, paypal):
    agent = Agent(store, guard, PreviewLLM(), EventBus(), "Kestrel Studio")

    async def all_items():
        for item in store.list_inbox():
            await agent.run_item(item["id"])

    run(all_items())
    by_source = {p["source_ref"]: p for p in store.list_payments()}
    assert by_source["task-1041"]["status"] == "executed"
    assert by_source["task-1038"]["status"] == "executed" and by_source["task-1038"]["amount"] == 1800
    assert by_source["mail-2207"]["status"] == "blocked"
    assert by_source["task-1044"]["status"] == "pending_approval"
    assert "mail-2211" not in by_source
    assert by_source["task-1047"]["status"] == "pending_approval"
    assert all(item["status"] == "done" for item in store.list_inbox())
