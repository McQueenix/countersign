"""The deterministic half of a countersignature.

Every rule here is plain code. No model output can loosen one of these limits:
the AI reviewer (reviewer.py) can only make a decision stricter.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from .models import MONEY_OUT_RAILS, Mandate, PaymentIntent, to_cents
from .store import Store

PASS, ESCALATE, DENY = "pass", "escalate", "deny"
SEVERITY = {PASS: 0, ESCALATE: 1, DENY: 2}
OUTCOME = {PASS: "allow", ESCALATE: "escalate", DENY: "deny"}


def check(rule: str, label: str, status: str, detail: str) -> dict[str, str]:
    return {"rule": rule, "label": label, "status": status, "detail": detail, "source": "policy"}


def usd(cents: int, currency: str = "USD") -> str:
    return f"{cents / 100:,.2f} {currency}"


def outcome_of(checks: list[dict[str, Any]]) -> str:
    worst = max((SEVERITY[c["status"]] for c in checks), default=0)
    return OUTCOME[[PASS, ESCALATE, DENY][worst]]


def evaluate(
    mandate: Optional[Mandate],
    intent: PaymentIntent,
    store: Store,
    evidence: str = "",
    today: Optional[date] = None,
    now: Optional[datetime] = None,
) -> list[dict[str, str]]:
    """Run every rule and return the full list, so the ledger shows what passed as well as what failed."""
    now = now or datetime.now(timezone.utc)
    today = today or now.date()
    checks: list[dict[str, str]] = []

    if mandate is None:
        return [check("mandate_active", "Mandate", DENY, "No mandate has been set, so the agent has no authority to move money.")]

    if not mandate.active:
        checks.append(check("mandate_active", "Mandate", DENY, "The mandate is paused."))
    elif mandate.expires_on and today > mandate.expires_on:
        checks.append(check("mandate_active", "Mandate", DENY, f"The mandate expired on {mandate.expires_on.isoformat()}."))
    else:
        until = f" until {mandate.expires_on.isoformat()}" if mandate.expires_on else ""
        checks.append(check("mandate_active", "Mandate", PASS, f"Active{until}."))

    rail_names = {"payout": "Payouts", "invoice": "Invoices", "purchase": "Purchases"}
    if mandate.rail_allowed(intent.rail):
        checks.append(check("rail_allowed", "Payment type", PASS, f"{rail_names[intent.rail]} are allowed."))
    else:
        checks.append(check("rail_allowed", "Payment type", DENY, f"{rail_names[intent.rail]} are not allowed by the mandate."))

    if intent.currency != mandate.currency:
        checks.append(check("currency", "Currency", DENY, f"Requested {intent.currency}, the mandate only covers {mandate.currency}."))

    cents = intent.cents
    if cents <= 0:
        checks.append(check("amount", "Amount", DENY, "The amount must be greater than zero."))
        return checks

    email = intent.counterparty_email
    money_out = intent.rail in MONEY_OUT_RAILS

    if money_out:
        cap = to_cents(mandate.per_payment_cap)
        if cents > cap:
            checks.append(check("per_payment_cap", "Per-payment cap", DENY, f"{usd(cents, intent.currency)} is over the {usd(cap, mandate.currency)} cap."))
        else:
            checks.append(check("per_payment_cap", "Per-payment cap", PASS, f"{usd(cents, intent.currency)} is within the {usd(cap, mandate.currency)} cap."))
    elif mandate.invoice_cap is not None:
        cap = to_cents(mandate.invoice_cap)
        if cents > cap:
            checks.append(check("invoice_cap", "Invoice cap", DENY, f"{usd(cents, intent.currency)} is over the {usd(cap, mandate.currency)} invoice cap."))
        else:
            checks.append(check("invoice_cap", "Invoice cap", PASS, f"Within the {usd(cap, mandate.currency)} invoice cap."))

    if email in mandate.blocked_payees:
        checks.append(check("blocked_payee", "Blocked list", DENY, f"{email} is on the blocked list."))

    if money_out:
        if email in mandate.allowed_payees:
            checks.append(check("known_payee", "Payee list", PASS, f"{email} is on the approved list."))
        elif mandate.unknown_payee == "deny":
            checks.append(check("known_payee", "Payee list", DENY, f"{email} is not on the approved list."))
        else:
            checks.append(check("known_payee", "Payee list", ESCALATE, f"{email} is not on the approved list, so the owner must sign."))

        for label, rule, window, budget in (
            ("Weekly budget", "weekly_budget", timedelta(days=7), mandate.weekly_budget),
            ("Daily budget", "daily_budget", timedelta(days=1), mandate.daily_budget),
        ):
            if budget is None:
                continue
            limit = to_cents(budget)
            used = store.committed_cents(now - window)
            if used + cents > limit:
                checks.append(check(rule, label, DENY, f"{usd(used)} already committed; this would exceed the {usd(limit, mandate.currency)} limit."))
            else:
                checks.append(check(rule, label, PASS, f"{usd(used + cents)} of {usd(limit, mandate.currency)} after this payment."))

    similar = store.similar_payments(intent.rail, email, cents)
    same_source = [p for p in similar if intent.source_ref and p["source_ref"] == intent.source_ref]
    if same_source:
        checks.append(check("duplicate", "Duplicate check", DENY, f"Already handled as {same_source[0]['id']} for the same work item."))
    elif similar:
        checks.append(check("duplicate", "Duplicate check", ESCALATE, f"Same amount to the same party in the last 24 hours ({similar[0]['id']}), so the owner must sign."))
    else:
        checks.append(check("duplicate", "Duplicate check", PASS, "No matching payment in the last 24 hours."))

    if money_out:
        limit = to_cents(mandate.auto_approve_limit)
        if cents > limit:
            checks.append(check("auto_approve_limit", "Sign-off threshold", ESCALATE, f"Over {usd(limit, mandate.currency)}, so the owner must sign."))
        else:
            checks.append(check("auto_approve_limit", "Sign-off threshold", PASS, f"At or under {usd(limit, mandate.currency)}."))

    if not evidence.strip():
        checks.append(check("evidence", "Source record", ESCALATE, "No source record was attached to this request."))
    else:
        checks.append(check("evidence", "Source record", PASS, "Linked to a source record."))

    return checks
