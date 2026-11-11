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
  "expires_on": "YYYY-MM-DD" or null
}}

Rules:
- Be conservative. If the owner did not clearly allow something, set it to false.
- If no approval threshold is given, set auto_approve_limit equal to per_payment_cap.
- If no per-payment cap is given, use the approval threshold; if neither is given, use 0.
- Only include email addresses that appear in the owner's text.
- Never invent limits that are looser than what the owner wrote."""


async def compile_mandate(llm: LLM, text: str) -> Mandate:
    text = (text or "").strip()
    if not text:
        raise LLMError("Write the mandate first.")
    raw: dict[str, Any] = await llm.json(SYSTEM.format(today=date.today().isoformat()), text)
    raw = {key: value for key, value in raw.items() if key in Mandate.model_fields}
    raw["text"] = text
    raw["active"] = True
    try:
        mandate = Mandate.model_validate(raw)
    except ValidationError as exc:
        raise LLMError(f"The model's rules did not validate: {exc.errors()[0].get('msg', 'invalid value')}") from exc
    # The sign-off threshold can never sit above the hard cap.
    if mandate.auto_approve_limit > mandate.per_payment_cap:
        mandate.auto_approve_limit = mandate.per_payment_cap
    return mandate
