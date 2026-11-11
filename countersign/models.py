"""Core data shapes: the owner's mandate and an agent's payment intent."""

from __future__ import annotations

import re
from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

Rail = Literal["payout", "invoice", "purchase"]
MONEY_OUT_RAILS = ("payout", "purchase")


def to_cents(amount: float) -> int:
    return int(round(float(amount) * 100))


def money(cents: int) -> str:
    """PayPal wants decimal strings such as "240.00"."""
    return f"{cents / 100:.2f}"


def _clean_emails(values: list[str]) -> list[str]:
    seen: list[str] = []
    for value in values or []:
        email = str(value).strip().lower()
        if EMAIL_RE.match(email) and email not in seen:
            seen.append(email)
    return seen


class Mandate(BaseModel):
    """What the owner allows the agent to do with money. Enforced in code, never by the model."""

    agent_name: str = "Ops Agent"
    text: str = ""
    active: bool = True
    currency: str = "USD"
    allow_payouts: bool = True
    allow_invoices: bool = True
    allow_purchases: bool = True
    per_payment_cap: float = Field(default=500, ge=0)
    auto_approve_limit: float = Field(default=250, ge=0)
    weekly_budget: Optional[float] = Field(default=1500, ge=0)
    daily_budget: Optional[float] = Field(default=None, ge=0)
    invoice_cap: Optional[float] = Field(default=5000, ge=0)
    allowed_payees: list[str] = Field(default_factory=list)
    blocked_payees: list[str] = Field(default_factory=list)
    unknown_payee: Literal["escalate", "deny"] = "escalate"
    expires_on: Optional[date] = None

    @field_validator("currency")
    @classmethod
    def _currency(cls, value: str) -> str:
        value = (value or "USD").strip().upper()
        return value if re.fullmatch(r"[A-Z]{3}", value) else "USD"

    @field_validator("allowed_payees", "blocked_payees", mode="before")
    @classmethod
    def _emails(cls, value):
        return _clean_emails(value if isinstance(value, list) else [])

    def rail_allowed(self, rail: str) -> bool:
        return {
            "payout": self.allow_payouts,
            "invoice": self.allow_invoices,
            "purchase": self.allow_purchases,
        }.get(rail, False)


class LineItem(BaseModel):
    name: str
    amount: float
    quantity: int = Field(default=1, ge=1)


class PaymentIntent(BaseModel):
    """A request from an agent to move money. Nothing here is trusted until it is countersigned."""

    rail: Rail
    counterparty_email: str
    counterparty_name: str = ""
    amount: float
    currency: str = "USD"
    purpose: str = ""
    justification: str = ""
    source_ref: Optional[str] = None
    items: list[LineItem] = Field(default_factory=list)

    @field_validator("counterparty_email")
    @classmethod
    def _email(cls, value: str) -> str:
        value = (value or "").strip().lower()
        if not EMAIL_RE.match(value):
            raise ValueError("counterparty_email must be an email address")
        return value

    @field_validator("currency")
    @classmethod
    def _currency(cls, value: str) -> str:
        return (value or "USD").strip().upper()

    @property
    def cents(self) -> int:
        return to_cents(self.amount)
