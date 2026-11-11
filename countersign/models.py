"""Core data shapes: the owner's mandate and an agent's payment intent."""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
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
    # Circuit breaker: freeze the agent after this many blocked requests in ten minutes (0 turns it off).
    quarantine_after: int = Field(default=3, ge=0, le=100)
    paused_reason: str = ""
    # Seconds an auto-approved payment waits, with an Undo button, before it goes to PayPal (0 turns it off).
    undo_seconds: int = Field(default=0, ge=0, le=600)
    # Hold a payment this many times above what the payee is usually paid (0 turns it off).
    unusual_factor: float = Field(default=3, ge=0, le=100)
    # Hold money-out requests beyond this many in one hour (None means no limit).
    max_per_hour: Optional[int] = Field(default=10, ge=1, le=10000)
    # Only pay inside these hours, for example "08:00-20:00", in the given time zone (None means any time).
    active_hours: Optional[str] = None
    timezone: str = "UTC"
    # The first payment to someone never paid before can never exceed this, even with the owner's signature.
    new_payee_cap: Optional[float] = Field(default=1000, ge=0)
    # Above this amount two different people must sign (None turns it off).
    dual_control_over: Optional[float] = Field(default=None, ge=0)
    # A temporary raise of the per-payment cap and the sign-off threshold, ending automatically.
    boost_limit: Optional[float] = Field(default=None, ge=0)
    boost_until: Optional[datetime] = None
    # When on, outside agents must present a key issued in the console.
    require_registered_agents: bool = False
    # Anyone at these domains counts as approved (a supplier's whole company, for example).
    allowed_domains: list[str] = Field(default_factory=list)
    # Weekly limits per spending category, for example {"Marketing": 300}.
    category_budgets: dict[str, float] = Field(default_factory=dict)
    # A payment nobody signs within this many hours is refused and its budget released (None keeps it waiting).
    hold_expires_hours: Optional[float] = Field(default=48, gt=0, le=24 * 30)
    # Refuse addresses that imitate an approved payee, and hold a known name at a new address.
    lookalike_guard: bool = True
    # Remind clients through PayPal when an invoice is still unpaid after this many days (None turns it off).
    remind_after_days: Optional[int] = Field(default=7, ge=1, le=90)
    # Someone never paid before must confirm their own PayPal address before the owner can sign.
    confirm_new_payees: bool = True

    @field_validator("allowed_domains", mode="before")
    @classmethod
    def _domains(cls, values):
        seen: list[str] = []
        for value in values or []:
            domain = str(value).strip().lower().lstrip("@")
            if re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", domain) and domain not in seen:
                seen.append(domain)
        return seen

    @field_validator("category_budgets", mode="before")
    @classmethod
    def _category_budgets(cls, value):
        clean: dict[str, float] = {}
        for name, amount in (value or {}).items():
            try:
                amount = float(amount)
            except (TypeError, ValueError):
                continue
            if str(name).strip() and amount >= 0:
                clean[str(name).strip()[:40]] = amount
        return clean

    def payee_approved(self, email: str) -> bool:
        return email in self.allowed_payees or email.rsplit("@", 1)[-1] in self.allowed_domains

    @field_validator("boost_until", mode="after")
    @classmethod
    def _aware(cls, value):
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    def boost_active(self, now: datetime) -> bool:
        return self.boost_limit is not None and self.boost_until is not None and now < self.boost_until

    def cap_at(self, now: datetime) -> float:
        return max(self.per_payment_cap, self.boost_limit) if self.boost_active(now) else self.per_payment_cap

    def sign_off_at(self, now: datetime) -> float:
        return max(self.auto_approve_limit, self.boost_limit) if self.boost_active(now) else self.auto_approve_limit

    @field_validator("active_hours", mode="before")
    @classmethod
    def _hours(cls, value):
        if value in (None, ""):
            return None
        text = str(value).strip().replace(" ", "").replace("–", "-")
        match = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-3]):([0-5]\d)", text)
        return text if match and text[:5] != text[6:] else None

    @field_validator("timezone", mode="before")
    @classmethod
    def _zone(cls, value):
        name = str(value or "UTC").strip()
        try:
            ZoneInfo(name)
            return name
        except (ZoneInfoNotFoundError, ValueError):
            return "UTC"

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
    category: str = ""

    @field_validator("category")
    @classmethod
    def _category(cls, value: str) -> str:
        return re.sub(r"\s+", " ", (value or "").strip())[:40]

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
