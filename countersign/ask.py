"""Ask your money: plain-language questions about what the agent did, answered only from the ledger.

Every answer names the payments it relied on, so the owner can open each one and check it.
The model sees the ledger and the mandate, nothing else, and is told to say so when the answer
is not there. Citations that do not exist in the ledger are dropped.
"""

from __future__ import annotations

from typing import Any, Optional

from .llm import LLM, LLMError
from .models import Mandate

SYSTEM = """You answer a business owner's questions about money their AI agent moved through PayPal.
Use only the MANDATE and LEDGER given to you. Never guess. If the answer is not in them, say so plainly.
Answer in at most three short sentences, in plain words, with exact amounts and names.
Reply with one JSON object and nothing else:
{"answer": "<your answer>", "cites": ["<payment ids you relied on>"]}"""

MAX_ROWS = 80


def _ledger_lines(payments: list[dict[str, Any]]) -> str:
    lines = []
    for p in payments[:MAX_ROWS]:
        stopped = ",".join(c["rule"] for c in p["checks"] if c["status"] != "pass") or "-"
        risk = (p.get("review") or {}).get("risk_score")
        paypal = (p.get("paypal") or {}).get("id") or "-"
        lines.append(
            f"{p['id']} | {p['created_at'][:16]} | {p['rail']} | {p['counterparty_name'] or '-'} <{p['counterparty_email']}> | "
            f"{p['amount']:.2f} {p['currency']} | {p['purpose'][:80]} | status={p['status']} | decided_by={p['decided_by'] or '-'} | "
            f"stopped_by={stopped} | ai_risk={risk if risk is not None else '-'} | paypal={paypal}"
        )
    return "\n".join(lines) or "(the ledger is empty)"


def _prompt(question: str, mandate: Optional[Mandate], payments: list[dict[str, Any]]) -> str:
    rules = mandate.model_dump_json(exclude={"text"}) if mandate else "(none)"
    return f"""MANDATE (owner's words)
{mandate.text if mandate else '(none)'}

MANDATE (rules as enforced)
{rules}

LEDGER (newest first; id | time | type | payee | amount | purpose | status | decided by | stopped by | AI risk | PayPal id)
{_ledger_lines(payments)}

QUESTION
{question}"""


async def answer(llm: LLM, question: str, mandate: Optional[Mandate], payments: list[dict[str, Any]]) -> dict[str, Any]:
    question = question.strip()[:500]
    if not question:
        raise LLMError("Type a question first.")
    raw = await llm.json(SYSTEM, _prompt(question, mandate, payments))
    text = str(raw.get("answer", "")).strip()[:1200]
    if not text:
        raise LLMError("The model did not give an answer.")
    known = {p["id"] for p in payments}
    cites = []
    for cite in raw.get("cites") or []:
        cite = str(cite).strip()
        if cite in known and cite not in cites:
            cites.append(cite)
    by_id = {p["id"]: p for p in payments}
    return {
        "question": question,
        "answer": text,
        "cites": [
            {"id": c, "label": f"{by_id[c]['counterparty_name'] or by_id[c]['counterparty_email']} {by_id[c]['amount']:.2f} {by_id[c]['currency']}",
             "status": by_id[c]["status"]}
            for c in cites[:8]
        ],
    }
