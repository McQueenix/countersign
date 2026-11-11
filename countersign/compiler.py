"""Turn the owner's plain-English mandate into enforceable rules.

The model only translates. The result is validated, shown to the owner,
and from then on enforced by policy.py without any model in the loop.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import ValidationError

from .llm import LLM, LLMError
from .models import Mandate

SYSTEM = """You convert a business owner's plain-English spending rules for an AI agent into JSON.
Today's date is {today}. Reply with one JSON object and nothing else, using exactly these keys:

{{
  "agent_name": string,              // what the owner calls the agent, default "Ops Agent"
  "currency": string,                // 3-letter code, default "USD"
  "allow_payouts": boolean,          // may it send money to people (contractors, staff)?
  "allow_invoices": boolean,         // may it bill clients?
  "allow_purchases": boolean,        // may it buy from vendors?
  "per_payment_cap": number,         // hard maximum for any single outgoing payment
  "auto_approve_limit": number,      // outgoing payments above this need the owner's approval
  "weekly_budget": number or null,   // total outgoing per 7 days
  "daily_budget": number or null,    // total outgoing per day
  "invoice_cap": number or null,     // largest invoice it may send
  "allowed_payees": [emails],        // people it may pay
  "blocked_payees": [emails],        // people it must never pay
  "unknown_payee": "escalate" | "deny",  // someone not on the list: ask the owner, or refuse
  "expires_on": "YYYY-MM-DD" or null,
  "undo_seconds": integer,           // seconds to wait, with an undo button, before an automatic payment is sent
  "unusual_factor": number,          // hold a payment this many times above what the payee is usually paid
  "max_per_hour": integer or null,   // most payment requests in one hour
  "active_hours": "HH:MM-HH:MM" or null,  // only pay inside these hours
  "timezone": string,                // IANA time zone for active_hours, for example "Asia/Kolkata"
  "quarantine_after": integer,       // freeze the agent after this many blocked requests in ten minutes
  "allowed_domains": [domains],      // companies whose whole domain is approved, for example "lumenstock.com"
  "category_budgets": {{category: number}},  // weekly limits per spending category, for example {{"Marketing": 300}}
  "new_payee_cap": number or null,   // most for the first payment to someone never paid before
  "dual_control_over": number or null,  // above this two different people must sign
  "hold_expires_hours": number or null  // refuse a payment nobody signs within this many hours
}}

Rules:
- Be conservative. If the owner did not clearly allow something, set it to false.
- If no approval threshold is given, set auto_approve_limit equal to per_payment_cap.
- If no per-payment cap is given, use the approval threshold; if neither is given, use 0.
- Only include email addresses that appear in the owner's text.
- Never invent limits that are looser than what the owner wrote.
- Leave out undo_seconds, unusual_factor, max_per_hour, active_hours, timezone, quarantine_after, allowed_domains,
  category_budgets, new_payee_cap, dual_control_over and hold_expires_hours unless the owner mentions them.
- Category names must be one of: Contractors, Client billing, Stock and software, Marketing, Travel, Refunds, Other."""


async def compile_mandate(llm: LLM, text: str) -> Mandate:
    text = (text or "").strip()
    if not text:
        raise LLMError("Write the mandate first.")
    raw: dict[str, Any] = await llm.json(SYSTEM.format(today=date.today().isoformat()), text)
    raw = {key: value for key, value in raw.items() if key in Mandate.model_fields}
    raw["text"] = text
    raw["active"] = True
    raw.pop("paused_reason", None)
    try:
        mandate = Mandate.model_validate(raw)
    except ValidationError as exc:
        raise LLMError(f"The model's rules did not validate: {exc.errors()[0].get('msg', 'invalid value')}") from exc
    # The sign-off threshold can never sit above the hard cap.
    if mandate.auto_approve_limit > mandate.per_payment_cap:
        mandate.auto_approve_limit = mandate.per_payment_cap
    return mandate
