"""Countersign as an MCP server, so any MCP-capable agent can ask for PayPal payments safely.

The agent gets three tools. None of them can move money on their own: each request goes
to a running Countersign console (COUNTERSIGN_URL), which applies the owner's mandate,
runs the AI risk review, and only then talks to PayPal.

    pip install mcp
    COUNTERSIGN_URL=http://localhost:8000 python -m countersign.mcp_server

Any MCP client can launch this over stdio.
"""

from __future__ import annotations

import os
from typing import Any, Literal

import httpx
try:  # mcp 2.x
    from mcp.server.mcpserver import MCPServer
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as MCPServer

BASE_URL = os.getenv("COUNTERSIGN_URL", "http://localhost:8000").rstrip("/")
AGENT_NAME = os.getenv("COUNTERSIGN_AGENT_NAME", "MCP agent")
AGENT_KEY = os.getenv("COUNTERSIGN_AGENT_KEY", "")  # issued in the console under Agents

mcp = MCPServer("countersign")


async def _call(method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    headers = {"X-Countersign-Key": AGENT_KEY} if AGENT_KEY else {}
    async with httpx.AsyncClient(timeout=120) as http:
        response = await http.request(method, f"{BASE_URL}{path}", json=body, headers=headers)
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail")
        except ValueError:
            detail = response.text[:300]
        return {"error": detail or f"Countersign returned {response.status_code}"}
    return response.json()


def _brief(payment: dict[str, Any]) -> dict[str, Any]:
    if "error" in payment:
        return payment
    return {
        "payment_id": payment["id"],
        "status": payment["status"],
        "decision": payment["decision"],
        "amount": payment["amount"],
        "currency": payment["currency"],
        "counterparty": payment["counterparty_email"],
        "reasons": [f"{c['label']}: {c['detail']}" for c in payment["checks"] if c["status"] != "pass"],
        "paypal": {k: v for k, v in (payment.get("paypal") or {}).items() if k in ("api", "id", "status", "item_status", "approve_url", "payer_url")},
    }


@mcp.tool()
async def request_payment(
    kind: Literal["payout", "invoice", "purchase"],
    counterparty_email: str,
    amount: float,
    purpose: str,
    justification: str,
    counterparty_name: str = "",
    currency: str = "USD",
) -> dict[str, Any]:
    """Ask Countersign to move money through PayPal on the owner's behalf.

    kind: "payout" sends money to a person, "invoice" bills a client, "purchase" buys from a vendor.
    The result says whether it was sent, is waiting for the owner's signature, or was blocked, and why.
    A held or blocked payment must not be retried, split, or re-routed.
    """
    payment = await _call(
        "POST",
        "/api/intents",
        {
            "rail": kind,
            "counterparty_email": counterparty_email,
            "counterparty_name": counterparty_name,
            "amount": amount,
            "currency": currency,
            "purpose": purpose,
            "justification": justification,
            "actor": AGENT_NAME,
        },
    )
    return _brief(payment)


@mcp.tool()
async def payment_status(payment_id: str) -> dict[str, Any]:
    """Look up a payment you requested earlier, including the latest status from PayPal."""
    return _brief(await _call("GET", f"/api/payments/{payment_id}"))


@mcp.tool()
async def read_mandate() -> dict[str, Any]:
    """Read the owner's spending rules, so you know what you may ask for before you ask."""
    return await _call("GET", "/api/mandate")


if __name__ == "__main__":
    mcp.run()
