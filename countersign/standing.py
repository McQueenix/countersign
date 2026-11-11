"""Standing orders: recurring payments the owner sets up once. Every run still goes through the guard."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .models import EMAIL_RE
from .store import Store, now_iso

KEY = "standing_orders"


class StandingOrders:
    def __init__(self, store: Store) -> None:
        self.store = store

    def list(self) -> list[dict[str, Any]]:
        return json.loads(self.store.get_kv(KEY) or "[]")

    def _save(self, orders: list[dict[str, Any]]) -> None:
        self.store.set_kv(KEY, json.dumps(orders))

    def get(self, order_id: str) -> Optional[dict[str, Any]]:
        return next((o for o in self.list() if o["id"] == order_id), None)

    def create(self, name: str, email: str, amount: float, every_days: int, purpose: str) -> dict[str, Any]:
        email = email.strip().lower()
        if not EMAIL_RE.match(email):
            raise ValueError("That is not an email address.")
        if not 0 < amount <= 100000:
            raise ValueError("Give an amount greater than zero.")
        every_days = max(1, min(int(every_days), 365))
        now = datetime.now(timezone.utc)
        order = {
            "id": "so_" + uuid.uuid4().hex[:8], "name": " ".join(name.split())[:60], "email": email, "amount": round(float(amount), 2),
            "every_days": every_days, "purpose": " ".join(purpose.split())[:120] or "Standing order", "active": True,
            "created_at": now_iso(), "next_run": (now + timedelta(days=every_days)).isoformat(timespec="seconds"), "runs": 0,
        }
        orders = self.list()
        orders.append(order)
        self._save(orders)
        self.store.audit("standing_order_created", "owner", {k: order[k] for k in ("name", "email", "amount", "every_days", "purpose")})
        return order

    def stop(self, order_id: str) -> dict[str, Any]:
        orders = self.list()
        for order in orders:
            if order["id"] == order_id:
                order["active"] = False
                self._save(orders)
                self.store.audit("standing_order_stopped", "owner", {"name": order["name"]})
                return order
        raise KeyError(order_id)

    def take_run(self, order_id: str) -> tuple[dict[str, Any], str]:
        """Count one run and move the next date on. Returns the order and a unique source reference for this run."""
        orders = self.list()
        for order in orders:
            if order["id"] == order_id:
                order["runs"] += 1
                order["next_run"] = (datetime.now(timezone.utc) + timedelta(days=order["every_days"])).isoformat(timespec="seconds")
                self._save(orders)
                return order, f"standing-{order['id']}-{order['runs']}"
        raise KeyError(order_id)

    def due(self) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc)
        return [o for o in self.list() if o["active"] and datetime.fromisoformat(o["next_run"]) <= now]

    def evidence(self, source_ref: str) -> str:
        order_id = "_".join(source_ref.split("-")[1:2])
        order = self.get(order_id) if order_id else None
        if not order:
            return ""
        return (
            "Channel: standing order (set up by the signed-in owner in the console)\n"
            f"From: You (owner)\nSubject: Standing order: {order['purpose']}\n\n"
            f"Pay {order['name']} <{order['email']}> {order['amount']:.2f} USD every {order['every_days']} day(s) for: {order['purpose']}. "
            f"Set up on {order['created_at'][:10]}."
        )
