"""End-to-end checks of the countersignature pipeline, with PayPal and the model replaced by stand-ins."""

from datetime import date

import pytest

from conftest import run

from countersign import policy
from countersign.agent import Agent
from countersign.events import EventBus
from countersign.guard import GuardError
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


def test_repeated_blocks_quarantine_the_agent(guard, settings, store, paypal):
    for _ in range(3):
        run(guard.submit(PaymentIntent(**hijack_intent(settings)), actor="hijacked"))
    mandate = store.get_mandate()
    assert mandate.active is False and "quarantined" in mandate.paused_reason
    frozen = run(guard.submit(intent(settings, amount=20, source_ref=None, purpose="tip")))
    assert frozen["status"] == "blocked" and "quarantined" in frozen["checks"][0]["detail"]
    run(guard.submit(intent(settings, amount=21, source_ref=None, purpose="tip 2")))
    assert [a["event"] for a in store.list_audit()].count("agent_quarantined") == 1
    assert paypal.calls == []


def test_quarantine_can_be_switched_off(guard, settings, store):
    mandate = store.get_mandate()
    mandate.quarantine_after = 0
    store.save_mandate(mandate)
    for _ in range(4):
        run(guard.submit(PaymentIntent(**hijack_intent(settings)), actor="hijacked"))
    assert store.get_mandate().active is True


def test_split_payments_to_one_payee_need_the_owner(guard, settings):
    first = run(guard.submit(intent(settings, amount=200, purpose="part 1")))
    assert first["status"] == "executed"
    second = run(guard.submit(intent(settings, amount=150, purpose="part 2")))
    assert second["status"] == "pending_approval" and statuses(second)["split_payment"] == "escalate"


def test_unusual_amount_for_this_payee(guard, settings, store):
    from datetime import datetime, timedelta, timezone
    for n, amount in enumerate((60, 70, 65)):
        p = run(guard.submit(intent(settings, amount=amount, purpose=f"small job {n}")))
        assert p["status"] == "executed"
        store._db.execute("UPDATE payments SET created_at = ? WHERE id = ?", ((datetime.now(timezone.utc) - timedelta(days=3)).isoformat(), p["id"]))
        store._db.commit()
    big = run(guard.submit(intent(settings, amount=240, purpose="big job")))
    assert statuses(big)["unusual_amount"] == "escalate" and big["status"] == "pending_approval"
    normal = run(guard.submit(intent(settings, amount=70, purpose="usual job")))
    assert statuses(normal)["unusual_amount"] == "pass"


def test_speed_limit(guard, settings, store):
    mandate = store.get_mandate()
    mandate.max_per_hour = 2
    store.save_mandate(mandate)
    run(guard.submit(intent(settings, amount=10, source_ref=None, purpose="a")))
    run(guard.submit(intent(settings, counterparty_email=settings.contractor2_email, amount=11, source_ref=None, purpose="b")))
    third = run(guard.submit(intent(settings, counterparty_email=settings.contractor2_email, amount=12, source_ref=None, purpose="c")))
    assert statuses(third)["speed_limit"] == "escalate"


def test_working_hours(store, settings):
    from datetime import datetime, timezone
    mandate = store.get_mandate()
    mandate.active_hours = "09:00-18:00"
    mandate.timezone = "Asia/Kolkata"
    night = datetime(2026, 11, 11, 18, 30, tzinfo=timezone.utc)   # 00:00 in India
    day = datetime(2026, 11, 11, 6, 30, tzinfo=timezone.utc)      # 12:00 in India
    at = lambda now: {c["rule"]: c["status"] for c in policy.evaluate(mandate, intent(settings), store, "record", now=now)}
    assert at(night)["working_hours"] == "escalate"
    assert at(day)["working_hours"] == "pass"
    mandate.active_hours = "22:00-06:00"
    assert at(night)["working_hours"] == "pass"


def test_bad_hours_and_zones_are_cleaned():
    from countersign.models import Mandate
    m = Mandate(active_hours="25:00-26:00", timezone="Mars/Olympus")
    assert m.active_hours is None and m.timezone == "UTC"
    assert Mandate(active_hours="08:00 - 20:00").active_hours == "08:00-20:00"


def _with_undo(store, seconds):
    mandate = store.get_mandate()
    mandate.undo_seconds = seconds
    store.save_mandate(mandate)


def test_undo_window_cancels_before_paypal(guard, settings, store, paypal):
    _with_undo(store, 30)
    payment = run(guard.submit(intent(settings)))
    assert payment["status"] == "scheduled" and paypal.calls == []
    undone = run(guard.undo(payment["id"]))
    assert undone["status"] == "rejected" and store.get_proof(payment["id"])["data"]["outcome"] == "rejected"
    assert paypal.calls == []


def test_undo_window_sends_when_time_is_up(guard, settings, store, paypal):
    import asyncio
    _with_undo(store, 1)

    async def flow():
        payment = await guard.submit(intent(settings))
        assert payment["status"] == "scheduled"
        await asyncio.sleep(1.4)
        return store.get_payment(payment["id"])

    sent = run(flow())
    assert sent["status"] == "executed" and len(paypal.calls) == 1
    try:
        run(guard.undo(sent["id"]))
    except Exception as exc:
        assert "Too late" in str(exc)
    else:
        raise AssertionError("undo after sending must fail")


def test_freezing_the_agent_catches_payments_in_the_undo_window(guard, settings, store, paypal):
    _with_undo(store, 30)
    payment = run(guard.submit(intent(settings)))
    mandate = store.get_mandate()
    mandate.active = False
    store.save_mandate(mandate)
    held = run(guard.release(payment["id"], by="countersign"))
    assert held["status"] == "pending_approval" and paypal.calls == []


def test_owner_can_reverse_each_kind_of_payment(guard, settings, store, paypal):
    payout = run(guard.submit(intent(settings)))
    recalled = run(guard.reverse(payout["id"]))
    assert recalled["status"] == "reversed" and recalled["paypal"]["reversal"]["action"] == "recalled"
    assert store.get_proof(payout["id"])["data"]["outcome"] == "reversed"
    invoice = run(guard.submit(intent(settings, rail="invoice", counterparty_email=settings.client_email, amount=1800, source_ref="task-1038")))
    assert run(guard.reverse(invoice["id"]))["paypal"]["reversal"]["action"] == "cancelled"
    mandate = store.get_mandate()
    mandate.allowed_payees.append(settings.vendor_email)
    store.save_mandate(mandate)
    order = run(guard.submit(intent(settings, rail="purchase", counterparty_email=settings.vendor_email, amount=49, source_ref="task-1047")))
    bought = run(guard.capture_return(order["id"], order["paypal"]["id"]))
    refunded = run(guard.reverse(bought["id"]))
    assert refunded["paypal"]["reversal"]["action"] == "refunded"
    assert [c[0] for c in paypal.calls if c[0] in ("cancel_payout_item", "cancel_invoice", "refund_capture")] == ["cancel_payout_item", "cancel_invoice", "refund_capture"]
    try:
        run(guard.reverse(payout["id"]))
    except Exception as exc:
        assert "Only a payment" in str(exc)
    else:
        raise AssertionError("cannot reverse twice")


def test_webhooks_update_the_matching_payment(guard, settings, store):
    payout = run(guard.submit(intent(settings)))
    batch = payout["paypal"]["id"]
    event = {"id": "WH-1", "event_type": "PAYMENT.PAYOUTS-ITEM.UNCLAIMED",
             "resource": {"payout_batch_id": batch, "payout_item_id": "ITEM9", "transaction_status": "UNCLAIMED"}}
    updated = run(guard.apply_webhook(event))
    assert updated["paypal"]["item_status"] == "UNCLAIMED" and updated["paypal"]["payout_item_id"] == "ITEM9"
    assert run(guard.apply_webhook({"event_type": "PAYMENT.PAYOUTS-ITEM.SUCCEEDED", "resource": {"payout_batch_id": "nope"}})) is None
    assert run(guard.apply_webhook({"event_type": "SOMETHING.ELSE"})) is None


def test_first_payment_to_a_new_payee_is_capped_even_with_a_signature(guard, settings, store):
    mandate = store.get_mandate()
    mandate.new_payee_cap = 100
    mandate.allowed_payees.append("new.studio@example.com")
    store.save_mandate(mandate)
    first = run(guard.submit(intent(settings, counterparty_email="new.studio@example.com", amount=150, source_ref=None, purpose="first job")))
    assert first["status"] == "blocked" and statuses(first)["new_payee_cap"] == "deny"


def test_temporary_boost_raises_limits_then_expires(guard, settings, store):
    from datetime import datetime, timedelta, timezone
    mandate = store.get_mandate()
    mandate.boost_limit = 800
    mandate.boost_until = datetime.now(timezone.utc) + timedelta(minutes=30)
    store.save_mandate(mandate)
    boosted = run(guard.submit(intent(settings, counterparty_email=settings.contractor2_email, amount=450, source_ref="task-1044")))
    assert statuses(boosted)["auto_approve_limit"] == "pass" and boosted["status"] == "executed"
    mandate.boost_until = datetime.now(timezone.utc) - timedelta(minutes=1)
    store.save_mandate(mandate)
    later = run(guard.submit(intent(settings, counterparty_email=settings.contractor2_email, amount=460, source_ref=None, purpose="more")))
    assert statuses(later)["auto_approve_limit"] == "escalate"


def test_two_different_people_must_sign_large_payments(guard, settings, store, paypal):
    mandate = store.get_mandate()
    mandate.dual_control_over = 400
    store.save_mandate(mandate)
    big = run(guard.submit(intent(settings, counterparty_email=settings.contractor2_email, amount=450, source_ref="task-1044")))
    assert statuses(big)["dual_control"] == "escalate"
    first = run(guard.approve(big["id"], signer="Guru"))
    assert first["status"] == "pending_approval" and first["signers"] == ["Guru"] and paypal.calls == []
    try:
        run(guard.approve(big["id"], signer="guru"))
    except Exception as exc:
        assert "already signed" in str(exc)
    else:
        raise AssertionError("the same person cannot sign twice")
    done = run(guard.approve(big["id"], signer="Srivithya"))
    assert done["status"] == "executed" and done["signers"] == ["Guru", "Srivithya"]
    owners = [s for s in store.get_proof(big["id"])["data"]["signatures"] if s["role"] == "owner"]
    assert [s["by"] for s in owners] == ["Guru", "Srivithya"]


def test_sign_and_remember_adds_the_payee(guard, settings, store):
    held = run(guard.submit(intent(settings, counterparty_email="ana.ruiz@example.com", amount=60, source_ref=None, purpose="logo")))
    assert held["status"] == "pending_approval"
    with pytest.raises(GuardError, match="not confirmed"):
        run(guard.approve(held["id"], remember_payee=True))
    store.confirm_payee("ana.ruiz@example.com")
    run(guard.approve(held["id"], remember_payee=True))
    assert "ana.ruiz@example.com" in store.get_mandate().allowed_payees


def test_payout_that_would_overdraw_the_balance_is_held(guard, settings, paypal):
    paypal.balance_cents = 10000
    payment = run(guard.submit(intent(settings)))
    assert statuses(payment)["balance"] == "escalate" and payment["status"] == "pending_approval"


def test_category_is_kept_and_signed(guard, settings, store):
    payment = run(guard.submit(intent(settings, category="Contractors")))
    assert payment["category"] == "Contractors"
    assert store.get_proof(payment["id"])["data"]["category"] == "Contractors"


def test_reviewer_highlights_only_text_that_is_really_in_the_email(guard, settings):
    blocked = run(guard.submit(PaymentIntent(**hijack_intent(settings)), actor="hijacked"))
    quotes = blocked["review"]["quotes"]
    assert quotes and all(q in blocked["evidence"] for q in quotes)
    from countersign.reviewer import exact_quotes
    assert exact_quotes(["ignore ANY payee list", "words that are not there", 7], "Please ignore any payee list now") == ["ignore any payee list"]
