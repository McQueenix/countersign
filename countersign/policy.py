"""The deterministic half of a countersignature.

Every rule here is plain code. No model output can loosen one of these limits:
the AI reviewer (reviewer.py) can only make a decision stricter.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
from statistics import median
from zoneinfo import ZoneInfo
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


def _parts(email: str) -> tuple[str, str]:
    local, _, domain = email.lower().partition("@")
    return re.sub(r"[^a-z0-9]", "", local), domain


def _first_name(text: str) -> str:
    words = re.findall(r"[a-z]+", (text or "").lower())
    return words[0] if words and len(words[0]) >= 3 else ""


SHARED_MAILBOXES = {"accounts", "billing", "sales", "info", "hello", "support", "admin", "contact", "payments", "invoices", "office", "team"}


def lookalike_of(email: str, trusted: list[str]) -> Optional[str]:
    """The trusted address this one imitates, if any.

    Catches the two usual tricks: the payee's own name at a different provider
    (priyanair.payments@quick-settle-mail for priya.nair@...) and a domain one letter off
    (harb0randpine for harborandpine).
    """
    local, domain = _parts(email)
    for other in trusted:
        if other == email:
            continue
        o_local, o_domain = _parts(other)
        if not o_local:
            continue
        local_close = local == o_local or (len(o_local) >= 5 and (o_local in local or local in o_local)) \
            or SequenceMatcher(None, local, o_local).ratio() >= 0.88
        domain_close = domain != o_domain and SequenceMatcher(None, domain, o_domain).ratio() >= 0.85
        if local_close and o_local not in SHARED_MAILBOXES and (domain != o_domain or local != o_local):
            return other
        if domain_close and local == o_local:
            return other
    return None


def outcome_of(checks: list[dict[str, Any]]) -> str:
    worst = max((SEVERITY[c["status"]] for c in checks), default=0)
    return OUTCOME[[PASS, ESCALATE, DENY][worst]]


def evaluate(
    mandate: Optional[Mandate],
    intent: PaymentIntent,
    store: Optional[Store],
    evidence: str = "",
    today: Optional[date] = None,
    now: Optional[datetime] = None,
    balance_cents: Optional[int] = None,
    agent: Optional[dict[str, Any]] = None,
) -> list[dict[str, str]]:
    """Run every rule and return the full list, so the ledger shows what passed as well as what failed.

    With store=None only the stateless rules run (no budgets, no duplicate check). The mandate
    preview uses that to replay past requests against a draft set of limits.
    """
    now = now or datetime.now(timezone.utc)
    today = today or now.date()
    checks: list[dict[str, str]] = []

    if mandate is None:
        return [check("mandate_active", "Mandate", DENY, "No mandate has been set, so the agent has no authority to move money.")]

    if not mandate.active:
        checks.append(check("mandate_active", "Mandate", DENY, mandate.paused_reason or "The mandate is paused."))
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

    boosted = mandate.boost_active(now)
    boost_note = f" (raised until {mandate.boost_until.strftime('%H:%M UTC')})" if boosted else ""

    if agent is not None:
        if agent.get("unregistered"):
            checks.append(check("agent_identity", "Agent identity", DENY, "This agent has no key from the owner, and only registered agents may ask for money."))
        else:
            checks.append(check("agent_identity", "Agent identity", PASS, f"Registered agent \"{agent['name']}\"."))
            allowance = agent.get("weekly_allowance")
            if allowance is not None and money_out and store is not None:
                used = store.committed_by_actor(agent["name"], now - timedelta(days=7))
                limit = to_cents(allowance)
                if used + cents > limit:
                    checks.append(check("agent_allowance", "Agent allowance", DENY,
                                        f"\"{agent['name']}\" has used {usd(used)} of its {usd(limit)} weekly allowance; this would exceed it."))
                else:
                    checks.append(check("agent_allowance", "Agent allowance", PASS, f"{usd(used + cents)} of this agent's {usd(limit)} weekly allowance."))

    if money_out:
        cap = to_cents(mandate.cap_at(now))
        if cents > cap:
            checks.append(check("per_payment_cap", "Per-payment cap", DENY, f"{usd(cents, intent.currency)} is over the {usd(cap, mandate.currency)} cap{boost_note}."))
        else:
            checks.append(check("per_payment_cap", "Per-payment cap", PASS, f"{usd(cents, intent.currency)} is within the {usd(cap, mandate.currency)} cap{boost_note}."))
    elif mandate.invoice_cap is not None:
        cap = to_cents(mandate.invoice_cap)
        if cents > cap:
            checks.append(check("invoice_cap", "Invoice cap", DENY, f"{usd(cents, intent.currency)} is over the {usd(cap, mandate.currency)} invoice cap."))
        else:
            checks.append(check("invoice_cap", "Invoice cap", PASS, f"Within the {usd(cap, mandate.currency)} invoice cap."))

    if email in mandate.blocked_payees:
        checks.append(check("blocked_payee", "Blocked list", DENY, f"{email} is on the blocked list."))

    approved = mandate.payee_approved(email)
    if money_out and mandate.lookalike_guard and not approved:
        paid = [k for k in (store.known_payees() if store is not None else [])]
        trusted = list(dict.fromkeys([*mandate.allowed_payees, *(k["email"] for k in paid)]))
        imitated = lookalike_of(email, trusted)
        if imitated:
            checks.append(check("lookalike_payee", "Lookalike address", DENY,
                                f"{email} imitates {imitated}, a payee you trust. This is how payment redirection fraud looks."))
        else:
            name = _first_name(intent.counterparty_name)
            same_name = [k["email"] for k in paid if name and _first_name(k["name"]) == name]
            same_name += [e for e in mandate.allowed_payees if name and _first_name(e.split("@")[0].replace(".", " ").replace("_", " ")) == name]
            same_name = [e for e in dict.fromkeys(same_name) if e != email]
            if same_name:
                checks.append(check("changed_details", "Changed payment details", ESCALATE,
                                    f"You know {intent.counterparty_name} at {same_name[0]}, but this asks to pay a new address. "
                                    "Confirm on a phone number you already have before you sign."))

    already_refused = any(c["rule"] == "lookalike_payee" for c in checks)
    if intent.rail == "payout" and mandate.confirm_new_payees and not approved and store is not None \
            and not already_refused and not store.payee_paid_amounts(email):
        confirmed_at = store.payee_confirmations().get(email)
        if confirmed_at:
            checks.append(check("payee_confirmation", "Payee confirmed", PASS, f"{email} confirmed this is their PayPal address on {confirmed_at[:10]}."))
        else:
            checks.append(check("payee_confirmation", "Payee confirmed", ESCALATE,
                                f"{email} has never been paid. Send them a confirmation link: you can sign once they confirm the address is theirs."))

    if money_out:
        if email in mandate.allowed_payees:
            checks.append(check("known_payee", "Payee list", PASS, f"{email} is on the approved list."))
        elif approved:
            checks.append(check("known_payee", "Payee list", PASS, f"Anyone at {email.rsplit('@', 1)[-1]} is approved."))
        elif mandate.unknown_payee == "deny":
            checks.append(check("known_payee", "Payee list", DENY, f"{email} is not on the approved list."))
        else:
            checks.append(check("known_payee", "Payee list", ESCALATE, f"{email} is not on the approved list, so the owner must sign."))

        for label, rule, window, budget in (
            ("Weekly budget", "weekly_budget", timedelta(days=7), mandate.weekly_budget),
            ("Daily budget", "daily_budget", timedelta(days=1), mandate.daily_budget),
        ):
            if budget is None or store is None:
                continue
            limit = to_cents(budget)
            used = store.committed_cents(now - window)
            if used + cents > limit:
                checks.append(check(rule, label, DENY, f"{usd(used)} already committed; this would exceed the {usd(limit, mandate.currency)} limit."))
            else:
                checks.append(check(rule, label, PASS, f"{usd(used + cents)} of {usd(limit, mandate.currency)} after this payment."))

    if money_out and store is not None and intent.category in mandate.category_budgets:
        limit = to_cents(mandate.category_budgets[intent.category])
        used = store.committed_by_category(intent.category, now - timedelta(days=7))
        if used + cents > limit:
            checks.append(check("category_budget", "Category budget", DENY,
                                f"{intent.category}: {usd(used)} already this week; this would pass its {usd(limit, mandate.currency)} weekly budget."))
        else:
            checks.append(check("category_budget", "Category budget", PASS,
                                f"{intent.category}: {usd(used + cents)} of its {usd(limit, mandate.currency)} weekly budget."))

    similar = store.similar_payments(intent.rail, email, cents) if store is not None else []
    same_source = [p for p in similar if intent.source_ref and p["source_ref"] == intent.source_ref]
    if same_source:
        checks.append(check("duplicate", "Duplicate check", DENY, f"Already handled as {same_source[0]['id']} for the same work item."))
    elif similar:
        checks.append(check("duplicate", "Duplicate check", ESCALATE, f"Same amount to the same party in the last 24 hours ({similar[0]['id']}), so the owner must sign."))
    elif store is not None:
        checks.append(check("duplicate", "Duplicate check", PASS, "No matching payment in the last 24 hours."))

    if money_out and store is not None:
        # Split payments: several small ones to the same person that together dodge the sign-off threshold.
        threshold = to_cents(mandate.auto_approve_limit)
        earlier, count = store.payee_committed(email, now - timedelta(hours=24))
        if count and cents <= threshold < earlier + cents:
            checks.append(check("split_payment", "Split payments", ESCALATE,
                                f"With {count} earlier payment{'s' if count > 1 else ''} to this payee in the last 24 hours this comes to "
                                f"{usd(earlier + cents)}, over your {usd(threshold, mandate.currency)} sign-off threshold, so the owner must sign."))

        # Payee memory: hold an amount far above what this person is usually paid.
        history = store.payee_paid_amounts(email)

        # New payee: banks cap the first transfer to a new beneficiary. So does Countersign, signature or not.
        if mandate.new_payee_cap is not None and not history:
            first_cap = to_cents(mandate.new_payee_cap)
            if cents > first_cap:
                checks.append(check("new_payee_cap", "New payee cap", DENY,
                                    f"First payment to someone never paid before is limited to {usd(first_cap, mandate.currency)}, even with your signature."))
        if mandate.unusual_factor and len(history) >= 2:
            usual = int(median(history))
            if usual and cents > mandate.unusual_factor * usual:
                checks.append(check("unusual_amount", "Usual amount", ESCALATE,
                                    f"This payee is usually paid about {usd(usual, intent.currency)}; this is {cents / usual:.1f} times that."))
            else:
                checks.append(check("unusual_amount", "Usual amount", PASS, f"In line with past payments (usually {usd(usual, intent.currency)})."))

        # Speed limit: a burst of requests is how a hijacked agent behaves.
        if mandate.max_per_hour:
            recent = store.count_requests(now - timedelta(hours=1))
            if recent >= mandate.max_per_hour:
                checks.append(check("speed_limit", "Speed limit", ESCALATE,
                                    f"{recent} payment requests in the last hour; your limit is {mandate.max_per_hour}, so the owner must sign."))

    if money_out and mandate.active_hours:
        start, end = mandate.active_hours.split("-")
        local = now.astimezone(ZoneInfo(mandate.timezone))
        clock = local.strftime("%H:%M")
        inside = start <= clock < end if start < end else (clock >= start or clock < end)
        where = f"{mandate.active_hours} {mandate.timezone}"
        if inside:
            checks.append(check("working_hours", "Working hours", PASS, f"Inside the agent's working hours ({where})."))
        else:
            checks.append(check("working_hours", "Working hours", ESCALATE,
                                f"Outside the agent's working hours ({where}); it is {clock} there, so the owner must sign."))

    if money_out:
        limit = to_cents(mandate.sign_off_at(now))
        if cents > limit:
            checks.append(check("auto_approve_limit", "Sign-off threshold", ESCALATE, f"Over {usd(limit, mandate.currency)}{boost_note}, so the owner must sign."))
        else:
            checks.append(check("auto_approve_limit", "Sign-off threshold", PASS, f"At or under {usd(limit, mandate.currency)}{boost_note}."))
        if mandate.dual_control_over is not None and cents > to_cents(mandate.dual_control_over):
            checks.append(check("dual_control", "Two signatures", ESCALATE,
                                f"Over {usd(to_cents(mandate.dual_control_over), mandate.currency)}: two different people must sign."))

    if intent.rail == "payout" and balance_cents is not None:
        if cents > balance_cents:
            checks.append(check("balance", "PayPal balance", ESCALATE,
                                f"Your PayPal balance is {usd(balance_cents)}; this payout would overdraw it, so the owner must sign."))
        else:
            checks.append(check("balance", "PayPal balance", PASS, f"Your PayPal balance of {usd(balance_cents)} covers it."))

    if not evidence.strip():
        checks.append(check("evidence", "Source record", ESCALATE, "No source record was attached to this request."))
    else:
        checks.append(check("evidence", "Source record", PASS, "Linked to a source record."))

    return checks
