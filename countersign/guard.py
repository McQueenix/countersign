"""The countersignature pipeline.

    agent's request -> policy (code) -> AI risk review -> decision -> PayPal -> ledger

An agent never talks to PayPal. It can only ask the guard, and the guard decides:
  allow     every rule passed and the review is clear  -> sent to PayPal now
  escalate  a rule or the review wants a human         -> waits for the owner's signature
  deny      a hard limit or a likely fraud             -> blocked, nothing is sent
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from . import policy, reviewer
from .events import EventBus
from .llm import LLM
from .models import PaymentIntent
from .paypal import PayPalClient, PayPalError
from .proof import Notary, mandate_fingerprint
from .standing import StandingOrders
from .store import Store, new_id, now_iso


class GuardError(Exception):
    pass


QUARANTINE_WINDOW = timedelta(minutes=10)


# Where a source record came from. Set by the server, never by the message itself,
# so an email cannot claim to be the owner.
CHANNELS = {
    "owner": "owner console (typed by the signed-in owner)",
    "task": "project board (internal system of record)",
    "email": "inbound email (unauthenticated, anyone can send one)",
    "standing": "standing order (set up by the signed-in owner in the console)",
}


class Guard:
    def __init__(
        self,
        store: Store,
        paypal: PayPalClient,
        llm: LLM,
        bus: EventBus,
        public_base_url: str,
        notary: Optional[Notary] = None,
    ) -> None:
        self.store = store
        self.notary = notary or Notary(store, "Countersign")
        self.standing = StandingOrders(store)
        self.paypal = paypal
        self.llm = llm
        self.bus = bus
        self.public_base_url = public_base_url.rstrip("/")
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        self._balance: tuple[float, Optional[int]] = (0.0, None)

    # ---- helpers -----------------------------------------------------------

    def _emit(self, payment: dict[str, Any]) -> dict[str, Any]:
        self.bus.publish("payment", payment)
        return payment

    def _evidence(self, source_ref: Optional[str]) -> str:
        if not source_ref:
            return ""
        if source_ref.startswith("standing-"):
            return self.standing.evidence(source_ref)
        item = self.store.get_inbox_item(source_ref)
        if not item:
            return ""
        channel = CHANNELS.get(item["kind"], "unknown channel (unauthenticated)")
        return f"Channel: {channel}\nFrom: {item['sender']}\nSubject: {item['subject']}\n\n{item['body']}"

    async def balance(self, fresh: bool = False) -> Optional[int]:
        """PayPal balance in cents, cached briefly. None when PayPal will not say (the check is then skipped)."""
        checked_at, cents = self._balance
        if not fresh and time.monotonic() - checked_at < 30:
            return cents
        try:
            cents = await self.paypal.get_balance("USD")
        except (PayPalError, AttributeError):
            cents = None
        self._balance = (time.monotonic(), cents)
        return cents

    def verify_url(self, payment_id: str) -> str:
        return f"{self.public_base_url}/verify/{payment_id}"

    def _seal(self, payment: dict[str, Any]) -> dict[str, Any]:
        """Issue the signed proof for a payment that has reached its final decision, then announce it."""
        self.notary.issue(payment)
        self._emit(payment)
        if payment["status"] == "blocked":
            self._watch_for_compromise()
        return payment

    def _watch_for_compromise(self) -> None:
        """Circuit breaker on the agent itself.

        One blocked request is the guard doing its job. Several in a few minutes means the agent is
        being steered by someone else, so it is frozen until the owner has looked. Requests that were
        blocked only because the agent was already frozen do not count.
        """
        mandate = self.store.get_mandate()
        if not mandate or not mandate.active or mandate.quarantine_after <= 0:
            return
        since = datetime.now(timezone.utc) - QUARANTINE_WINDOW
        blocks = [
            p for p in self.store.recent_blocks(since)
            if not any(c["rule"] == "mandate_active" and c["status"] == "deny" for c in p["checks"])
        ]
        if len(blocks) < mandate.quarantine_after:
            return
        mandate.active = False
        mandate.paused_reason = (
            f"Agent quarantined: {len(blocks)} of its requests were blocked in the last 10 minutes, "
            "which looks like someone else is steering it. Nothing will be paid until you review the ledger and put the mandate back in force."
        )
        self.store.save_mandate(mandate, actor="countersign")
        self.store.audit("agent_quarantined", "countersign", {"blocked": [p["id"] for p in blocks]})
        self.bus.publish("mandate", mandate.model_dump(mode="json"))
        self.bus.publish("alert", {"kind": "quarantine", "text": mandate.paused_reason})

    def _spawn(self, coroutine) -> None:
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ---- request -----------------------------------------------------------

    async def submit(self, intent: PaymentIntent, actor: str = "agent", agent: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Take a payment request through the whole pipeline and return the final record."""
        balance = await self.balance() if intent.rail == "payout" else None
        mandate = self.store.get_mandate()
        mandate_hash = mandate_fingerprint(mandate)
        evidence = self._evidence(intent.source_ref)

        # Policy and the budget reservation happen together, so two requests
        # arriving at once cannot both squeeze under the same limit.
        async with self._lock:
            checks = policy.evaluate(mandate, intent, self.store, evidence, balance_cents=balance, agent=agent)
            outcome = policy.outcome_of(checks)
            payment = self.store.insert_payment(
                {
                    "id": new_id(),
                    "created_at": now_iso(),
                    "updated_at": now_iso(),
                    "actor": actor,
                    "rail": intent.rail,
                    "counterparty_email": intent.counterparty_email,
                    "counterparty_name": intent.counterparty_name,
                    "amount_cents": intent.cents,
                    "currency": intent.currency,
                    "purpose": intent.purpose,
                    "justification": intent.justification,
                    "source_ref": intent.source_ref,
                    "evidence": evidence,
                    "items": [item.model_dump() for item in intent.items],
                    "status": "blocked" if outcome == "deny" else "reviewing",
                    "decision": "blocked_policy" if outcome == "deny" else "",
                    "checks": checks,
                    "review": {},
                    "paypal": {},
                    "decided_by": "policy" if outcome == "deny" else "",
                    "note": "",
                    "mandate_hash": mandate_hash,
                    "category": intent.category,
                }
            )
        payment_id = payment["id"]
        self.store.audit("requested", actor, intent.model_dump(mode="json"), payment_id)
        self.store.audit("policy_evaluated", "policy", {"outcome": outcome, "checks": checks, "mandate_sha256": mandate_hash}, payment_id)
        if outcome == "deny":
            if evidence and any(c["rule"] == "lookalike_payee" and c["status"] == "deny" for c in checks):
                # Already refused, but a lookalike address usually arrives with a planted story. The AI still
                # reads the message, for the record only, so the owner can see exactly which words were the trap.
                review = await reviewer.review(self.llm, mandate, intent, evidence, {"payee_history": 0, "recent": []})
                payment = self.store.update_payment(payment_id, checks=checks + [reviewer.as_check(review)], review=review)
                self.store.audit("ai_reviewed", "ai_reviewer", {**review, "for_the_record": True}, payment_id)
            return self._seal(payment)
        self._emit(payment)

        context = {
            "payee_history": self.store.payee_history(intent.counterparty_email),
            "recent": [p for p in self.store.list_payments() if p["id"] != payment_id][:20],
        }
        review = await reviewer.review(self.llm, mandate, intent, evidence, context)
        checks = checks + [reviewer.as_check(review)]
        outcome = policy.outcome_of(checks)
        self.store.audit("ai_reviewed", "ai_reviewer", review, payment_id)

        if outcome == "deny":
            payment = self.store.update_payment(
                payment_id, status="blocked", decision="blocked_ai", decided_by="ai_reviewer", checks=checks, review=review
            )
            self.store.audit("blocked", "ai_reviewer", {"risk_score": review.get("risk_score")}, payment_id)
            return self._seal(payment)

        if outcome == "escalate":
            payment = self.store.update_payment(
                payment_id, status="pending_approval", decision="needs_owner", decided_by="", checks=checks, review=review
            )
            self.store.audit("held_for_owner", "countersign", {"reasons": [c["label"] for c in checks if c["status"] == "escalate"]}, payment_id)
            return self._emit(payment)

        delay = mandate.undo_seconds if mandate else 0
        if delay and intent.rail in ("payout", "invoice"):
            release_at = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat(timespec="milliseconds")
            payment = self.store.update_payment(
                payment_id, status="scheduled", decision="auto_approved", decided_by="policy+ai", checks=checks, review=review,
                paypal={"scheduled_for": release_at, "attempt": 0},
            )
            self.store.audit("scheduled", "countersign", {"seconds": delay, "release_at": release_at}, payment_id)
            self._emit(payment)
            self._spawn(self._release_later(payment_id, delay))
            return payment

        payment = self.store.update_payment(
            payment_id, status="executing", decision="auto_approved", decided_by="policy+ai", checks=checks, review=review
        )
        self.store.audit("auto_approved", "countersign", {}, payment_id)
        self._emit(payment)
        return await self._execute(payment)

    async def run_standing(self, order_id: str) -> dict[str, Any]:
        """One run of a standing order, as an ordinary request through every check."""
        order = self.standing.get(order_id)
        if not order or not order["active"]:
            raise GuardError("No such standing order, or it was stopped.")
        order, source_ref = self.standing.take_run(order_id)
        intent = PaymentIntent(
            rail="payout", counterparty_email=order["email"], counterparty_name=order["name"], amount=order["amount"],
            purpose=f"{order['purpose']} (standing order, run {order['runs']})", justification="Standing order set up by the owner.",
            source_ref=source_ref, category="Contractors",
        )
        return await self.submit(intent, actor="standing order")

    # ---- undo window ---------------------------------------------------------

    async def _release_later(self, payment_id: str, delay: float) -> None:
        await asyncio.sleep(max(0.0, delay))
        try:
            await self.release(payment_id, by="countersign")
        except GuardError:
            pass

    def resume_scheduled(self) -> None:
        """After a restart, payments still in their undo window keep their original release time."""
        now = datetime.now(timezone.utc)
        for payment in self.store.list_payments():
            if payment["status"] == "scheduled":
                release_at = datetime.fromisoformat((payment.get("paypal") or {}).get("scheduled_for", now.isoformat()))
                self._spawn(self._release_later(payment["id"], (release_at - now).total_seconds()))

    async def release(self, payment_id: str, by: str = "owner") -> dict[str, Any]:
        """End the undo window: send now. If the agent was frozen in the meantime, hold it for the owner instead."""
        async with self._lock:
            payment = self.store.get_payment(payment_id)
            if not payment:
                raise GuardError("No such payment.")
            if payment["status"] != "scheduled":
                return payment
            mandate = self.store.get_mandate()
            if not mandate or not mandate.active:
                payment = self.store.update_payment(
                    payment_id, status="pending_approval", decision="needs_owner", decided_by="",
                    note="The agent was frozen during the undo window, so this is held for you.",
                )
                self.store.audit("held_for_owner", "countersign", {"reasons": ["Agent frozen during the undo window"]}, payment_id)
                return self._emit(payment)
            payment = self.store.update_payment(payment_id, status="executing")
        self.store.audit("released", by, {}, payment_id)
        self._emit(payment)
        return await self._execute(payment)

    async def undo(self, payment_id: str) -> dict[str, Any]:
        async with self._lock:
            payment = self.store.get_payment(payment_id)
            if not payment:
                raise GuardError("No such payment.")
            if payment["status"] != "scheduled":
                raise GuardError("Too late to undo: this payment has already left the undo window.")
            payment = self.store.update_payment(
                payment_id, status="rejected", decision="owner_rejected", decided_by="owner", note="Undone in the undo window.",
                paypal={},
            )
        self.store.audit("owner_undid", "owner", {}, payment_id)
        return self._seal(payment)

    # ---- owner actions -----------------------------------------------------

    async def approve(self, payment_id: str, note: str = "", signer: str = "Owner", remember_payee: bool = False,
                      after_invoice: Optional[str] = None) -> dict[str, Any]:
        signer = (signer or "Owner").strip()[:40] or "Owner"
        async with self._lock:
            payment = self.store.get_payment(payment_id)
            if not payment:
                raise GuardError("No such payment.")
            if payment["status"] != "pending_approval":
                raise GuardError("This payment is not waiting for a signature.")
            current = self.store.get_mandate()
            if current and not current.active and "emergency stop" in current.paused_reason:
                raise GuardError("Everything is frozen by the emergency stop. Put the mandate back in force before you sign.")
            if any(c["rule"] == "payee_confirmation" and c["status"] == "escalate" for c in payment["checks"]) \
                    and payment["counterparty_email"] not in self.store.payee_confirmations():
                raise GuardError(f"{payment['counterparty_email']} has not confirmed this address yet. Send them the confirmation link first.")
            invoice = None
            if after_invoice:
                invoice = self.store.get_payment(after_invoice)
                if payment["rail"] != "payout" or not invoice or invoice["rail"] != "invoice" or invoice["status"] != "executed":
                    raise GuardError("Choose an invoice that has been sent to a client.")
                if (invoice.get("paypal") or {}).get("status") == "PAID":
                    invoice = None
            signers = list(payment.get("signers") or [])
            if signer.lower() in (s.lower() for s in signers):
                raise GuardError(f"{signer} has already signed. A different person must give the second signature.")
            signers.append(signer)
            needs_two = any(c["rule"] == "dual_control" and c["status"] == "escalate" for c in payment["checks"])
            if needs_two and len(signers) < 2:
                payment = self.store.update_payment(payment_id, signers=signers, note=f"Signed by {signer}. Waiting for a second person.")
                self.store.audit("first_signature", signer, {"note": note[:500]}, payment_id)
                return self._emit(payment)
            if invoice:
                client = invoice["counterparty_name"] or invoice["counterparty_email"]
                payment = self.store.update_payment(
                    payment_id, status="awaiting_client", decision="owner_approved", decided_by="owner", signers=signers,
                    note=f"Signed. Goes to PayPal as soon as {client} pays invoice {invoice['id']}.",
                    paypal={"release_on_invoice": invoice["id"]},
                )
                self.store.audit("pay_when_paid", signer, {"invoice": invoice["id"], "signers": signers}, payment_id)
                return self._emit(payment)
            payment = self.store.update_payment(
                payment_id, status="executing", decision="owner_approved", decided_by="owner", note=note[:500], signers=signers
            )
        self.store.audit("owner_approved", signer, {"note": note[:500], "signers": signers}, payment_id)
        if remember_payee:
            mandate = self.store.get_mandate()
            if mandate and payment["counterparty_email"] not in mandate.allowed_payees:
                mandate.allowed_payees.append(payment["counterparty_email"])
                self.store.save_mandate(mandate, actor=signer)
                self.bus.publish("mandate", mandate.model_dump(mode="json"))
        self._emit(payment)
        return await self._execute(payment)

    async def reject(self, payment_id: str, note: str = "") -> dict[str, Any]:
        payment = self.store.get_payment(payment_id)
        if not payment:
            raise GuardError("No such payment.")
        if payment["status"] != "pending_approval":
            raise GuardError("This payment is not waiting for a signature.")
        payment = self.store.update_payment(
            payment_id, status="rejected", decision="owner_rejected", decided_by="owner", note=note[:500]
        )
        self.store.audit("owner_rejected", "owner", {"note": note[:500]}, payment_id)
        return self._seal(payment)

    # ---- pay when paid ---------------------------------------------------------

    async def release_paid_dependents(self, invoice: dict[str, Any]) -> list[dict[str, Any]]:
        """The client paid: send the contractor payouts that were waiting on this invoice."""
        if (invoice.get("paypal") or {}).get("status") != "PAID":
            return []
        mandate = self.store.get_mandate()
        if not mandate or not mandate.active:
            return []  # frozen: the payouts keep waiting until the owner unfreezes
        released = []
        for payment in self.store.list_payments():
            if payment["status"] == "awaiting_client" and (payment.get("paypal") or {}).get("release_on_invoice") == invoice["id"]:
                async with self._lock:
                    fresh = self.store.get_payment(payment["id"])
                    if fresh["status"] != "awaiting_client":
                        continue
                    fresh = self.store.update_payment(payment["id"], status="executing", paypal={}, note="The client paid, so this went to PayPal.")
                self.store.audit("client_paid_released", "countersign", {"invoice": invoice["id"]}, payment["id"])
                self._emit(fresh)
                released.append(await self._execute(fresh))
        return released

    async def watch_invoices(self) -> None:
        """Poll invoices that payouts are waiting on, for when no webhook is configured."""
        waiting_on = {(p.get("paypal") or {}).get("release_on_invoice") for p in self.store.list_payments() if p["status"] == "awaiting_client"}
        for invoice_id in waiting_on - {None}:
            await self.sync(invoice_id)

    # ---- invoice reminders ------------------------------------------------------

    async def remind(self, payment_id: str, automatic: bool = False) -> dict[str, Any]:
        payment = self.store.get_payment(payment_id)
        if not payment or payment["rail"] != "invoice" or payment["status"] != "executed":
            raise GuardError("Only a sent invoice can be chased.")
        paypal = payment.get("paypal") or {}
        if paypal.get("status") in ("PAID", "CANCELLED", "MARKED_AS_PAID"):
            raise GuardError("This invoice is already settled.")
        days = max(1, (datetime.now(timezone.utc) - datetime.fromisoformat(payment["created_at"])).days)
        note = (f"A friendly reminder that invoice {paypal.get('invoice_number', '')} for {payment['purpose']} "
                f"({payment['amount_cents'] / 100:,.2f} {payment['currency']}) is still open after {days} day{'s' if days > 1 else ''}. Thank you.")
        try:
            await self.paypal.remind_invoice(paypal["id"], note)
        except PayPalError as exc:
            raise GuardError(f"PayPal: {exc.message}") from exc
        reminders = int(paypal.get("reminders", 0)) + 1
        payment = self.store.update_payment(payment_id, paypal={**paypal, "reminders": reminders, "last_reminded": now_iso()})
        self.store.audit("invoice_reminded", "countersign" if automatic else "owner", {"reminders": reminders}, payment_id)
        return self._emit(payment)

    async def remind_due(self) -> list[dict[str, Any]]:
        """Chase unpaid invoices on the owner's schedule, at most three times each."""
        mandate = self.store.get_mandate()
        if not mandate or not mandate.remind_after_days:
            return []
        gap = timedelta(days=mandate.remind_after_days)
        now = datetime.now(timezone.utc)
        sent = []
        for payment in self.store.list_payments():
            paypal = payment.get("paypal") or {}
            if payment["rail"] != "invoice" or payment["status"] != "executed" or paypal.get("status") in ("PAID", "CANCELLED", "MARKED_AS_PAID"):
                continue
            last = datetime.fromisoformat(paypal.get("last_reminded") or payment["created_at"])
            if int(paypal.get("reminders", 0)) < 3 and now - last >= gap:
                try:
                    sent.append(await self.remind(payment["id"], automatic=True))
                except GuardError:
                    continue
        return sent

    async def expire_holds(self) -> list[dict[str, Any]]:
        """Refuse payments nobody signed in time, so a forgotten queue cannot sit on the budget forever."""
        mandate = self.store.get_mandate()
        if not mandate or not mandate.hold_expires_hours:
            return []
        hours = mandate.hold_expires_hours
        expired = []
        async with self._lock:
            for payment in self.store.stale_holds(datetime.now(timezone.utc) - timedelta(hours=hours)):
                payment = self.store.update_payment(
                    payment["id"], status="rejected", decision="expired", decided_by="countersign",
                    note=f"Nobody signed within {hours:g} hours, so it was refused and its budget released.",
                )
                self.store.audit("hold_expired", "countersign", {"hours": hours}, payment["id"])
                expired.append(payment)
        return [self._seal(p) for p in expired]

    async def freeze_all(self, reason: str = "") -> dict[str, Any]:
        """The owner's emergency stop: pause the mandate and cancel everything still in the undo window."""
        mandate = self.store.get_mandate()
        if not mandate:
            raise GuardError("No mandate has been set.")
        cancelled = []
        async with self._lock:
            mandate.active = False
            mandate.paused_reason = (reason or "Frozen by you with the emergency stop.")[:300] + \
                " Nothing will be paid until you put the mandate back in force."
            self.store.save_mandate(mandate, actor="owner")
            for payment in self.store.list_payments():
                if payment["status"] == "scheduled":
                    payment = self.store.update_payment(
                        payment["id"], status="rejected", decision="owner_rejected", decided_by="owner",
                        note="Cancelled by the emergency stop before it reached PayPal.", paypal={},
                    )
                    self.store.audit("owner_undid", "owner", {"via": "emergency stop"}, payment["id"])
                    cancelled.append(payment)
        self.store.audit("emergency_stop", "owner", {"cancelled": [p["id"] for p in cancelled]})
        for payment in cancelled:
            self._seal(payment)
        self.bus.publish("mandate", mandate.model_dump(mode="json"))
        self.bus.publish("alert", {"kind": "quarantine", "text": mandate.paused_reason})
        waiting = sum(1 for p in self.store.list_payments() if p["status"] == "pending_approval")
        return {"cancelled": len(cancelled), "still_waiting": waiting, "mandate": mandate.model_dump(mode="json")}

    async def retry(self, payment_id: str) -> dict[str, Any]:
        payment = self.store.get_payment(payment_id)
        if not payment:
            raise GuardError("No such payment.")
        if payment["status"] != "failed":
            raise GuardError("Only a failed payment can be retried.")
        payment = self.store.update_payment(payment_id, status="executing")
        self.store.audit("retry", "owner", {}, payment_id)
        self._emit(payment)
        return await self._execute(payment)

    # ---- PayPal ------------------------------------------------------------

    def _request_key(self, payment: dict[str, Any]) -> dict[str, Any]:
        """The idempotency key for PayPal.

        If the last attempt got a definite error from PayPal, nothing was created and a
        fresh key is used. If the outcome is unknown (network failure, restart), the same
        key is reused so PayPal replays the original result instead of paying twice.
        """
        attempt = int((payment.get("paypal") or {}).get("attempt", 0))
        key = payment["id"] if attempt == 0 else f"{payment['id']}-r{attempt}"
        return {**payment, "id": key, "verify_url": self.verify_url(payment["id"])}

    async def _execute(self, payment: dict[str, Any]) -> dict[str, Any]:
        payment_id = payment["id"]
        previous = payment.get("paypal") or {}
        attempt = int(previous.get("attempt", 0))
        keyed = self._request_key(payment)
        try:
            if payment["rail"] == "payout":
                result = await self.paypal.create_payout(keyed)
                status = "executed"
            elif payment["rail"] == "invoice":
                result = await self.paypal.create_and_send_invoice(keyed)
                status = "executed"
            else:
                result = await self.paypal.create_order(
                    keyed,
                    return_url=f"{self.public_base_url}/paypal/return?payment_id={payment_id}",
                    cancel_url=f"{self.public_base_url}/paypal/cancel?payment_id={payment_id}",
                )
                status = "awaiting_payer"
        except PayPalError as exc:
            definite = 400 <= exc.status < 500
            error = {"api": {"payout": "Payouts", "invoice": "Invoicing", "purchase": "Orders"}[payment["rail"]], "error": exc.as_dict(), "attempt": attempt + 1 if definite else attempt}
            updated = self.store.update_payment(payment_id, status="failed", paypal=error, note=f"PayPal: {exc.message}"[:500])
            self.store.audit("paypal_failed", "paypal", exc.as_dict(), payment_id)
            return self._emit(updated)

        result["attempt"] = attempt
        if payment["rail"] == "payout":
            self._balance = (0.0, None)  # read the balance again before the next payout
        updated = self.store.update_payment(payment_id, status=status, paypal=result, note=payment.get("note", ""))
        self.store.audit("paypal_executed" if status == "executed" else "paypal_order_created", "paypal", result, payment_id)
        if status == "executed":
            self._seal(updated)
        else:
            self._emit(updated)
        if payment["rail"] == "payout":
            self._spawn(self._follow_payout(payment_id))
        return updated

    async def _follow_payout(self, payment_id: str) -> None:
        """Payout batches settle a few seconds after they are accepted. Follow up so the ledger shows the outcome."""
        for delay in (3, 6, 12, 25):
            await asyncio.sleep(delay)
            try:
                payment = await self.sync(payment_id)
            except (GuardError, PayPalError):
                return
            if (payment.get("paypal") or {}).get("status") in {"SUCCESS", "DENIED", "CANCELED"}:
                return

    async def sync(self, payment_id: str) -> dict[str, Any]:
        """Refresh a payment's status from PayPal, the source of truth."""
        payment = self.store.get_payment(payment_id)
        if not payment:
            raise GuardError("No such payment.")
        paypal = payment.get("paypal") or {}
        if not paypal.get("id"):
            return payment
        if payment["rail"] == "payout":
            fresh = await self.paypal.get_payout(paypal["id"])
        elif payment["rail"] == "invoice":
            fresh = await self.paypal.get_invoice(paypal["id"])
        else:
            fresh = await self.paypal.get_order(paypal["id"])
            fresh["approve_url"] = fresh.get("approve_url") or paypal.get("approve_url", "")
        merged = {**paypal, **{k: v for k, v in fresh.items() if v not in ("", None)}}
        if merged == paypal:
            return payment
        updated = self.store.update_payment(payment_id, paypal=merged)
        self.store.audit("paypal_status", "paypal", {"status": merged.get("status"), "item_status": merged.get("item_status", "")}, payment_id)
        self._emit(updated)
        if updated["rail"] == "invoice":
            await self.release_paid_dependents(updated)
        return updated

    # ---- reversals and webhooks ------------------------------------------------

    async def reverse(self, payment_id: str) -> dict[str, Any]:
        """Take a sent payment back through PayPal: recall a payout, refund a purchase, cancel an invoice."""
        payment = self.store.get_payment(payment_id)
        if not payment:
            raise GuardError("No such payment.")
        if payment["status"] != "executed":
            raise GuardError("Only a payment that PayPal accepted can be reversed.")
        paypal = payment.get("paypal") or {}
        if payment["rail"] == "payout":
            if not paypal.get("payout_item_id"):
                payment = await self.sync(payment_id)
                paypal = payment.get("paypal") or {}
            if not paypal.get("payout_item_id"):
                raise GuardError("PayPal has not given this payout an item id yet. Try again in a few seconds.")
            result = await self.paypal.cancel_payout_item(paypal["payout_item_id"])
        elif payment["rail"] == "purchase":
            if not paypal.get("capture_id"):
                raise GuardError("This purchase has no capture to refund.")
            result = await self.paypal.refund_capture(paypal["capture_id"], payment_id)
        else:
            if paypal.get("status") == "PAID":
                raise GuardError("The client has already paid this invoice, so it cannot be cancelled.")
            result = await self.paypal.cancel_invoice(paypal["id"])
        updated = self.store.update_payment(
            payment_id, status="reversed", paypal={**paypal, "reversal": result},
            note=f"{result['action'].capitalize()} through PayPal by the owner.",
        )
        self.store.audit("owner_reversed", "owner", result, payment_id)
        return self._seal(updated)

    async def apply_webhook(self, event: dict[str, Any]) -> Optional[dict[str, Any]]:
        """Record what PayPal says happened next (claimed, paid, refunded) on the matching payment."""
        kind = str(event.get("event_type", ""))
        res = event.get("resource") or {}
        if kind.startswith("PAYMENT.PAYOUTS-ITEM."):
            key = res.get("payout_batch_id")
            changes = {"item_status": res.get("transaction_status"), "payout_item_id": res.get("payout_item_id"),
                       "transaction_id": res.get("transaction_id")}
        elif kind.startswith("PAYMENT.PAYOUTSBATCH."):
            header = res.get("batch_header") or {}
            key, changes = header.get("payout_batch_id"), {"status": header.get("batch_status")}
        elif kind.startswith("INVOICING.INVOICE."):
            invoice = res.get("invoice") or res
            key, changes = invoice.get("id"), {"status": invoice.get("status")}
        elif kind.startswith("PAYMENT.CAPTURE."):
            related = (res.get("supplementary_data") or {}).get("related_ids") or {}
            key, changes = related.get("order_id"), {"capture_id": res.get("id"), "capture_status": res.get("status")}
        elif kind.startswith("CHECKOUT.ORDER."):
            key, changes = res.get("id"), {"status": res.get("status")}
        else:
            return None
        if not key:
            return None
        match = next((p for p in self.store.list_payments() if (p.get("paypal") or {}).get("id") == key), None)
        if not match:
            return None
        merged = {**match["paypal"], **{k: v for k, v in changes.items() if v}, "last_event": kind}
        updated = self.store.update_payment(match["id"], paypal=merged)
        self.store.audit("paypal_webhook", "paypal", {"event_type": kind, "event_id": event.get("id", "")}, match["id"])
        self._emit(updated)
        if updated["rail"] == "invoice":
            await self.release_paid_dependents(updated)
        return updated

    async def capture_return(self, payment_id: str, order_id: str) -> dict[str, Any]:
        """The owner approved the order inside PayPal and was sent back to us. Capture it."""
        payment = self.store.get_payment(payment_id)
        if not payment or payment["rail"] != "purchase":
            raise GuardError("No such purchase.")
        paypal = payment.get("paypal") or {}
        if payment["status"] != "awaiting_payer" or paypal.get("id") != order_id:
            raise GuardError("This order is not waiting for approval.")
        try:
            result = await self.paypal.capture_order(order_id, payment_id)
        except PayPalError as exc:
            updated = self.store.update_payment(payment_id, status="failed", paypal={**paypal, "error": exc.as_dict()}, note=f"PayPal: {exc.message}"[:500])
            self.store.audit("paypal_failed", "paypal", exc.as_dict(), payment_id)
            return self._emit(updated)
        merged = {**paypal, **{k: v for k, v in result.items() if v not in ("", None)}}
        updated = self.store.update_payment(payment_id, status="executed", paypal=merged)
        self.store.audit("paypal_executed", "paypal", result, payment_id)
        return self._seal(updated)

    async def cancel_return(self, payment_id: str) -> Optional[dict[str, Any]]:
        payment = self.store.get_payment(payment_id)
        if not payment or payment["status"] != "awaiting_payer":
            return payment
        updated = self.store.update_payment(payment_id, status="rejected", decision="owner_rejected", decided_by="owner", note="Cancelled at PayPal checkout.")
        self.store.audit("owner_rejected", "owner", {"note": "Cancelled at PayPal checkout."}, payment_id)
        return self._seal(updated)
