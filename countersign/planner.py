"""Payment run planner: which held payments to send today, decided by optimisation, explained by AI.

The decision is framed first: from the payments waiting for the owner's signature, choose the set to
send now and the set to hold, so the business keeps its cash buffer and pays what matters most.

1. The owner states goals in plain words. The model turns them into a validated goal (priorities,
   cash buffer, risk appetite, horizon). That is its only part in the decision.
2. A forecast of what clients will pay within the horizon is built from each client's own payment
   history, and it improves as more invoices are paid (plan, act, check, adjust).
3. A small exact optimiser chooses the plan:

       maximise   sum(v_i x_i) - lambda * ( sum(r_i x_i) + sum_payee(S_p^2) / cash )
       subject to sum(a_i x_i) <= cash available - buffer
                  sum(x_i)     <= most payments per run
                  x_i = 0 for payments that cannot be signed yet
                  x_i in {0, 1}

   v_i is the value of paying now (priority of its category, times the amount, rising the longer it
   has waited), r_i its expected risk (the AI review's risk score and whether the payee is new), and
   the quadratic term penalises putting much of the cash into one payee in one run.
4. The plan is shown with its trade-off curve (value against risk for different risk appetites), the
   same plan under three cash scenarios, and the limits that held it back (sensitivity).
5. The model explains the chosen plan in plain words, citing payments. It cannot change it.

The same inputs always give the same plan, and every plan records a fingerprint of its inputs.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator

from .agent import CATEGORIES
from .llm import LLM, LLMError
from .models import MONEY_OUT_RAILS

RISK_LAMBDA = {"low": 3.0, "medium": 1.0, "high": 0.3}
DEFAULT_PRIORITY = {"Contractors": 5, "Refunds": 4, "Stock and software": 3, "Travel": 2, "Marketing": 2, "Other": 2, "Client billing": 1}
FRONTIER = [0.0, 0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0]
SCENARIOS = {"expected": "Clients pay as they usually do", "cautious": "No client pays in time", "hopeful": "Every open invoice is paid"}
EXACT_LIMIT = 18
LEAVING = ("scheduled", "executing", "awaiting_payer", "awaiting_client")


class PlannerGoal(BaseModel):
    text: str = ""
    buffer: float = Field(default=500, ge=0)
    risk_appetite: Literal["low", "medium", "high"] = "medium"
    priorities: dict[str, int] = Field(default_factory=lambda: dict(DEFAULT_PRIORITY))
    max_payments: int = Field(default=10, ge=1, le=50)
    horizon_days: int = Field(default=7, ge=1, le=60)
    scenario: Literal["expected", "cautious", "hopeful"] = "expected"

    @field_validator("priorities", mode="before")
    @classmethod
    def _priorities(cls, value):
        clean = dict(DEFAULT_PRIORITY)
        for name, weight in (value or {}).items():
            match = next((c for c in CATEGORIES if c.lower() == str(name).strip().lower()), None)
            try:
                weight = int(round(float(weight)))
            except (TypeError, ValueError):
                continue
            if match:
                clean[match] = max(1, min(5, weight))
        return clean


GOAL_SYSTEM = """You turn a small-business owner's goals for paying their bills into JSON for an optimiser.
Reply with one JSON object and nothing else, with exactly these keys:
{
  "buffer": number,            // cash to always keep in PayPal, in dollars; default 500
  "risk_appetite": "low" | "medium" | "high",   // how much risk the owner accepts; careful or safe means low
  "priorities": {category: 1-5},  // 5 = pay first. Categories: Contractors, Client billing, Stock and software, Marketing, Travel, Refunds, Other
  "max_payments": integer,     // most payments in one run; default 10
  "horizon_days": integer,     // how many days ahead to count on client payments; default 7
  "scenario": "expected" | "cautious" | "hopeful"  // cautious if the owner does not want to count on clients paying
}
Only set what the owner says or clearly implies. Never invent a buffer lower than the owner wrote."""

EXPLAIN_SYSTEM = """You explain a payment plan to a small-business owner in plain words.
The plan was chosen by an optimiser; you cannot change it. Reply with one JSON object:
{"summary": "at most four short sentences: what is paid today, what waits and why, and what cash is left", "cites": ["payment ids you mention"]}
Use only the facts given. Name people, not ids, in the summary."""


def canonical_sha(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


async def compile_goal(llm: LLM, text: str) -> PlannerGoal:
    text = (text or "").strip()
    if not text:
        raise LLMError("Write your goals first.")
    raw = await llm.json(GOAL_SYSTEM, text)
    raw = {k: v for k, v in raw.items() if k in PlannerGoal.model_fields and k != "text"}
    goal = PlannerGoal.model_validate({**raw, "text": text[:1000]})
    return goal


# ---- forecast: what clients will pay, learned from their own history --------------------------

def _paid_at(store, payment_id: str) -> Optional[datetime]:
    for entry in reversed(store.list_audit(payment_id)):
        data = entry.get("data") or {}
        if entry["event"] in ("paypal_status", "paypal_webhook") and data.get("status") == "PAID":
            return datetime.fromisoformat(entry["ts"])
    return None


def client_history(store, payments: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """For each client, how long their paid invoices took to be paid."""
    history: dict[str, dict[str, Any]] = {}
    for p in payments:
        if p["rail"] != "invoice" or p["status"] != "executed":
            continue
        row = history.setdefault(p["counterparty_email"], {"paid": 0, "open": 0, "days": []})
        if (p.get("paypal") or {}).get("status") == "PAID":
            paid = _paid_at(store, p["id"])
            row["paid"] += 1
            if paid:
                row["days"].append(max(0.0, (paid - datetime.fromisoformat(p["created_at"])).total_seconds() / 86400))
        else:
            row["open"] += 1
    for row in history.values():
        row["avg_days"] = round(sum(row["days"]) / len(row["days"]), 1) if row["days"] else None
    return history


def forecast_inflows(payments: list[dict[str, Any]], history: dict[str, dict[str, Any]], horizon_days: int, now: datetime) -> list[dict[str, Any]]:
    out = []
    for p in payments:
        paypal = p.get("paypal") or {}
        if p["rail"] != "invoice" or p["status"] != "executed" or paypal.get("status") in ("PAID", "CANCELLED", "MARKED_AS_PAID"):
            continue
        h = history.get(p["counterparty_email"]) or {}
        age = (now - datetime.fromisoformat(p["created_at"])).total_seconds() / 86400
        if h.get("avg_days") is not None:
            due_in = max(0.0, h["avg_days"] - age)
            chance = 0.9 if due_in <= horizon_days else 0.3
            basis = f"usually pays in {h['avg_days']:g} days"
        else:
            chance = 0.5
            basis = "no payment history yet"
        out.append({"id": p["id"], "client": p["counterparty_name"] or p["counterparty_email"], "amount": p["amount_cents"] / 100,
                    "chance": chance, "basis": basis})
    return out


# ---- the optimisation model -------------------------------------------------------------------

def _candidates(payments: list[dict[str, Any]], goal: PlannerGoal, confirmed: dict[str, str], trust: dict[str, int], now: datetime) -> list[dict[str, Any]]:
    items = []
    for p in payments:
        if p["status"] != "pending_approval" or p["rail"] not in MONEY_OUT_RAILS:
            continue
        amount = p["amount_cents"] / 100
        waited = max(0.0, (now - datetime.fromisoformat(p["created_at"])).total_seconds() / 86400)
        category = p.get("category") or "Other"
        priority = goal.priorities.get(category, 2)
        risk_score = (p.get("review") or {}).get("risk_score") or 0
        new_payee = trust.get(p["counterparty_email"], 0) == 0
        blocked_reason = ""
        if any(c["rule"] == "payee_confirmation" and c["status"] == "escalate" for c in p["checks"]) and p["counterparty_email"] not in confirmed:
            blocked_reason = "the payee has not confirmed their address yet"
        items.append({
            "id": p["id"], "who": p["counterparty_name"] or p["counterparty_email"], "email": p["counterparty_email"],
            "amount": amount, "category": category, "priority": priority, "waited_days": round(waited, 1),
            "risk_score": risk_score, "new_payee": new_payee,
            "two_signatures": any(c["rule"] == "dual_control" and c["status"] == "escalate" for c in p["checks"]),
            "value": round(priority * amount * (1 + 0.05 * min(waited, 20)), 2),
            "risk": round(amount * risk_score / 100 + (0.2 * amount if new_payee else 0), 2),
            "blocked": blocked_reason,
        })
    return sorted(items, key=lambda i: (-i["value"], i["id"]))


def _score(chosen: list[dict[str, Any]], lam: float, cash: float) -> tuple[float, float, float]:
    value = sum(i["value"] for i in chosen)
    by_payee: dict[str, float] = {}
    for i in chosen:
        by_payee[i["email"]] = by_payee.get(i["email"], 0) + i["amount"]
    risk = sum(i["risk"] for i in chosen) + sum(s * s for s in by_payee.values()) / max(cash, 1.0)
    return value - lam * risk, value, risk


def solve(items: list[dict[str, Any]], cash: float, max_payments: int, lam: float) -> dict[str, Any]:
    """Exact branch and bound for up to EXACT_LIMIT payments, greedy beyond. Deterministic."""
    pool = [i for i in items if not i["blocked"] and i["amount"] <= cash]
    exact = len(pool) <= EXACT_LIMIT
    if not exact:
        pool = pool[:EXACT_LIMIT]
    best = {"obj": 0.0, "set": []}
    suffix = [0.0] * (len(pool) + 1)
    for k in range(len(pool) - 1, -1, -1):
        suffix[k] = suffix[k + 1] + pool[k]["value"]

    def walk(k: int, chosen: list[dict[str, Any]], spent: float) -> None:
        obj, _, _ = _score(chosen, lam, cash)
        if obj > best["obj"] + 1e-9:  # strict improvement only, so the visiting order settles ties: same inputs, same plan
            best["obj"], best["set"] = obj, list(chosen)
        if k == len(pool) or obj + suffix[k] <= best["obj"] + 1e-9:
            return
        item = pool[k]
        if spent + item["amount"] <= cash + 1e-9 and len(chosen) < max_payments:
            chosen.append(item)
            walk(k + 1, chosen, spent + item["amount"])
            chosen.pop()
        walk(k + 1, chosen, spent)

    walk(0, [], 0.0)
    obj, value, risk = _score(best["set"], lam, cash)
    return {"ids": sorted(i["id"] for i in best["set"]), "objective": round(obj, 2), "value": round(value, 2),
            "risk": round(risk, 2), "amount": round(sum(i["amount"] for i in best["set"]), 2), "exact": exact}


def _reasons(items: list[dict[str, Any]], chosen: set[str], cash: float, max_payments: int, goal: PlannerGoal) -> list[dict[str, Any]]:
    spent = sum(i["amount"] for i in items if i["id"] in chosen)
    rows = []
    for i in items:
        if i["id"] in chosen:
            why = f"Pay today: {i['category']} has priority {i['priority']} of 5, waiting {i['waited_days']:g} days, AI risk {i['risk_score']}."
            if i["two_signatures"]:
                why += " A second person must also sign."
        elif i["blocked"]:
            why = f"Wait: {i['blocked']}."
        elif i["amount"] > cash - spent + 1e-9:
            why = f"Wait: paying it now would take cash below your ${goal.buffer:,.2f} buffer."
        elif len(chosen) >= max_payments:
            why = f"Wait: the run is limited to {max_payments} payments."
        elif any(o["email"] == i["email"] and o["id"] in chosen for o in items):
            why = f"Wait: {i['who']} is already being paid today, and one person getting this much of today's cash adds concentration risk."
        else:
            why = "Wait: the risk outweighs paying it today at your risk appetite" + (" (never paid before)" if i["new_payee"] else "") + "."
        rows.append({**i, "pay_today": i["id"] in chosen, "why": why})
    return rows


def _sensitivity(items: list[dict[str, Any]], chosen: set[str], cash: float, max_payments: int) -> list[str]:
    notes = []
    spent = sum(i["amount"] for i in items if i["id"] in chosen)
    waiting = [i for i in items if i["id"] not in chosen and not i["blocked"]]
    short = sorted((i["amount"] - (cash - spent), i) for i in waiting if i["amount"] > cash - spent + 1e-9)
    if short:
        gap, item = short[0]
        notes.append(f"Cash is the binding limit: ${gap:,.2f} more would also let you pay {item['who']} (${item['amount']:,.2f}).")
    if len(chosen) >= max_payments and waiting:
        notes.append(f"The {max_payments}-payment limit per run is binding.")
    if not notes:
        notes.append("No limit is binding: everything worth paying at your risk appetite fits.")
    return notes


def _reliance(start: float, leaving: float, amount: float, inflow: float, goal: PlannerGoal) -> list[str]:
    now_after = start - leaving - amount
    if inflow > 0 and now_after < goal.buffer:
        return [f"This plan counts on ${inflow:,.2f} expected from clients. Until they pay, cash would be ${now_after:,.2f}, "
                f"below your ${goal.buffer:,.2f} buffer. Choose the cautious scenario to plan with the cash you have."]
    return []


def build_plan(*, payments: list[dict[str, Any]], store, goal: PlannerGoal, balance: Optional[float], confirmed: dict[str, str],
               trust: dict[str, int], now: Optional[datetime] = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    history = client_history(store, payments)
    inflows = forecast_inflows(payments, history, goal.horizon_days, now)
    leaving = sum(p["amount_cents"] for p in payments if p["rail"] in MONEY_OUT_RAILS and p["status"] in LEAVING) / 100
    items = _candidates(payments, goal, confirmed, trust, now)
    start = balance or 0.0

    def inflow_for(scenario: str) -> float:
        return {"expected": sum(f["amount"] * f["chance"] for f in inflows), "cautious": 0.0,
                "hopeful": sum(f["amount"] for f in inflows)}[scenario]

    def cash_for(scenario: str) -> float:
        return round(max(0.0, start + inflow_for(scenario) - leaving - goal.buffer), 2)

    lam = RISK_LAMBDA[goal.risk_appetite]
    cash = cash_for(goal.scenario)
    plan = solve(items, cash, goal.max_payments, lam)
    chosen = set(plan["ids"])

    frontier, seen = [], set()
    for point_lam in FRONTIER:
        point = solve(items, cash, goal.max_payments, point_lam)
        key = tuple(point["ids"])
        if key in seen:
            continue
        seen.add(key)
        frontier.append({"lambda": point_lam, "value": point["value"], "risk": point["risk"], "amount": point["amount"],
                         "count": len(point["ids"]), "ids": point["ids"], "chosen": key == tuple(plan["ids"])})
    frontier.sort(key=lambda p: (p["risk"], p["value"]))

    scenarios = []
    for name, label in SCENARIOS.items():
        s_cash = cash_for(name)
        s_plan = solve(items, s_cash, goal.max_payments, lam)
        scenarios.append({"scenario": name, "label": label, "cash_available": s_cash, "count": len(s_plan["ids"]),
                          "amount": s_plan["amount"], "ids": s_plan["ids"], "current": name == goal.scenario})

    inputs = {"goal": goal.model_dump(), "balance": start, "leaving": leaving, "inflows": inflows,
              "items": [{k: i[k] for k in ("id", "amount", "value", "risk", "blocked", "email")} for i in items]}
    return {
        "created_at": now.isoformat(timespec="seconds"),
        "goal": goal.model_dump(),
        "balance": start,
        "already_leaving": leaving,
        "inflows": inflows,
        "cash_available": cash,
        "plan": plan,
        "rows": _reasons(items, chosen, cash, goal.max_payments, goal),
        "frontier": frontier,
        "scenarios": scenarios,
        "sensitivity": _sensitivity(items, chosen, cash, goal.max_payments) + _reliance(start, leaving, plan["amount"], inflow_for(goal.scenario), goal),
        "expected_inflow": round(inflow_for(goal.scenario), 2),
        "cash_after": round(start + inflow_for(goal.scenario) - leaving - plan["amount"], 2),
        "cash_after_before_clients_pay": round(start - leaving - plan["amount"], 2),
        "client_history": {k: {"paid": v["paid"], "avg_days": v["avg_days"]} for k, v in history.items()},
        "inputs_sha256": canonical_sha(inputs),
    }


async def explain(llm: LLM, result: dict[str, Any]) -> Optional[dict[str, Any]]:
    facts = {
        "pay_today": [{"id": r["id"], "who": r["who"], "amount": r["amount"], "why": r["why"]} for r in result["rows"] if r["pay_today"]],
        "wait": [{"id": r["id"], "who": r["who"], "amount": r["amount"], "why": r["why"]} for r in result["rows"] if not r["pay_today"]],
        "cash_after_plan_once_clients_pay": result["cash_after"], "cash_before_clients_pay": result["cash_after_before_clients_pay"],
        "buffer": result["goal"]["buffer"], "limits": result["sensitivity"],
    }
    if not facts["pay_today"] and not facts["wait"]:
        return None
    try:
        raw = await llm.json(EXPLAIN_SYSTEM, "PLAN\n" + json.dumps(facts, default=str))
    except LLMError as exc:
        return {"summary": f"The written explanation is unavailable: {exc}", "cites": []}
    known = {r["id"] for r in result["rows"]}
    return {"summary": str(raw.get("summary", ""))[:900], "cites": [c for c in (raw.get("cites") or []) if c in known][:8]}


def check_plan(record: dict[str, Any], payments: dict[str, dict[str, Any]], balance: Optional[float]) -> dict[str, Any]:
    """The check step: what the plan expected against what then happened."""
    planned = record["result"]["plan"]["ids"]
    sent = [i for i in planned if (payments.get(i) or {}).get("status") == "executed"]
    expected_in = sum(f["amount"] * f["chance"] for f in record["result"]["inflows"])
    received = sum(f["amount"] for f in record["result"]["inflows"] if ((payments.get(f["id"]) or {}).get("paypal") or {}).get("status") == "PAID")
    return {
        "planned": len(planned), "sent": len(sent),
        "planned_amount": record["result"]["plan"]["amount"],
        "sent_amount": round(sum(payments[i]["amount_cents"] for i in sent) / 100, 2),
        "expected_inflow": round(expected_in, 2), "received_inflow": round(received, 2),
        "predicted_cash_after": record["result"]["cash_after"],
        "balance_now": balance,
        "forecast_error": round(received - expected_in, 2),
    }
