"""SQLite persistence: the mandate, payment requests, the work inbox and a hash-chained audit log."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .models import MONEY_OUT_RAILS, Mandate

GENESIS = "0" * 64

# Money that is spent or reserved. Counted against budgets so a queue of
# pending approvals cannot be used to overshoot a limit.
COMMITTED = ("reviewing", "pending_approval", "executing", "awaiting_payer", "executed")

SCHEMA = """
CREATE TABLE IF NOT EXISTS mandate (id INTEGER PRIMARY KEY CHECK (id = 1), data TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS payments (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    rail TEXT NOT NULL,
    counterparty_email TEXT NOT NULL,
    counterparty_name TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    currency TEXT NOT NULL,
    purpose TEXT NOT NULL,
    justification TEXT NOT NULL,
    source_ref TEXT,
    evidence TEXT NOT NULL,
    items TEXT NOT NULL,
    status TEXT NOT NULL,
    decision TEXT NOT NULL,
    checks TEXT NOT NULL,
    review TEXT NOT NULL,
    paypal TEXT NOT NULL,
    decided_by TEXT NOT NULL,
    note TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    payment_id TEXT,
    event TEXT NOT NULL,
    actor TEXT NOT NULL,
    data TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS inbox (
    id TEXT PRIMARY KEY,
    position INTEGER NOT NULL,
    kind TEXT NOT NULL,
    sender TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    status TEXT NOT NULL,
    summary TEXT NOT NULL
);
"""

JSON_FIELDS = ("items", "checks", "review", "paypal")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def new_id() -> str:
    return "cs_" + uuid.uuid4().hex[:16]


def _digest(prev_hash: str, record: dict[str, Any]) -> str:
    canonical = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256((prev_hash + canonical).encode()).hexdigest()


class Store:
    def __init__(self, path: str = "countersign.db") -> None:
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.commit()

    # ---- mandate -----------------------------------------------------------

    def get_mandate(self) -> Optional[Mandate]:
        with self._lock:
            row = self._db.execute("SELECT data FROM mandate WHERE id = 1").fetchone()
        return Mandate.model_validate_json(row["data"]) if row else None

    def save_mandate(self, mandate: Mandate, actor: str = "owner") -> Mandate:
        with self._lock:
            self._db.execute(
                "INSERT INTO mandate (id, data, updated_at) VALUES (1, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at",
                (mandate.model_dump_json(), now_iso()),
            )
            self._db.commit()
        self.audit("mandate_updated", actor, mandate.model_dump(mode="json"))
        return mandate

    # ---- payments ----------------------------------------------------------

    def _payment(self, row: sqlite3.Row) -> dict[str, Any]:
        record = dict(row)
        for field in JSON_FIELDS:
            record[field] = json.loads(record[field])
        record["amount"] = record["amount_cents"] / 100
        return record

    def insert_payment(self, record: dict[str, Any]) -> dict[str, Any]:
        row = {**record}
        for field in JSON_FIELDS:
            row[field] = json.dumps(row.get(field) or ({} if field in ("review", "paypal") else []))
        columns = (
            "id created_at updated_at actor rail counterparty_email counterparty_name amount_cents "
            "currency purpose justification source_ref evidence items status decision checks review "
            "paypal decided_by note"
        ).split()
        with self._lock:
            self._db.execute(
                f"INSERT INTO payments ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
                [row.get(column) for column in columns],
            )
            self._db.commit()
        return self.get_payment(record["id"])  # type: ignore[return-value]

    def update_payment(self, payment_id: str, **fields: Any) -> dict[str, Any]:
        fields["updated_at"] = now_iso()
        for field in JSON_FIELDS:
            if field in fields:
                fields[field] = json.dumps(fields[field])
        assignments = ", ".join(f"{name} = ?" for name in fields)
        with self._lock:
            self._db.execute(
                f"UPDATE payments SET {assignments} WHERE id = ?", [*fields.values(), payment_id]
            )
            self._db.commit()
        return self.get_payment(payment_id)  # type: ignore[return-value]

    def get_payment(self, payment_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._db.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone()
        return self._payment(row) if row else None

    def list_payments(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM payments ORDER BY created_at DESC").fetchall()
        return [self._payment(row) for row in rows]

    def committed_cents(self, since: datetime, exclude_id: Optional[str] = None) -> int:
        """Money-out total that is spent or reserved since the given time."""
        marks = ", ".join("?" * len(COMMITTED))
        rails = ", ".join("?" * len(MONEY_OUT_RAILS))
        with self._lock:
            row = self._db.execute(
                f"SELECT COALESCE(SUM(amount_cents), 0) AS total FROM payments "
                f"WHERE status IN ({marks}) AND rail IN ({rails}) AND created_at >= ? AND id != ?",
                [*COMMITTED, *MONEY_OUT_RAILS, since.isoformat(timespec="milliseconds"), exclude_id or ""],
            ).fetchone()
        return int(row["total"])

    def similar_payments(self, rail: str, email: str, cents: int, hours: int = 24) -> list[dict[str, Any]]:
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="milliseconds")
        marks = ", ".join("?" * len(COMMITTED))
        with self._lock:
            rows = self._db.execute(
                f"SELECT * FROM payments WHERE rail = ? AND counterparty_email = ? AND amount_cents = ? "
                f"AND status IN ({marks}) AND created_at >= ?",
                [rail, email, cents, *COMMITTED, since],
            ).fetchall()
        return [self._payment(row) for row in rows]

    def payee_history(self, email: str) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) AS n FROM payments WHERE counterparty_email = ? AND status = 'executed'",
                (email,),
            ).fetchone()
        return int(row["n"])

    # ---- audit chain -------------------------------------------------------

    def audit(self, event: str, actor: str, data: dict[str, Any], payment_id: Optional[str] = None) -> dict[str, Any]:
        with self._lock:
            last = self._db.execute("SELECT hash FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
            prev_hash = last["hash"] if last else GENESIS
            record = {"ts": now_iso(), "payment_id": payment_id, "event": event, "actor": actor, "data": data}
            digest = _digest(prev_hash, record)
            cursor = self._db.execute(
                "INSERT INTO audit (ts, payment_id, event, actor, data, prev_hash, hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (record["ts"], payment_id, event, actor, json.dumps(data, sort_keys=True), prev_hash, digest),
            )
            self._db.commit()
            return {**record, "seq": cursor.lastrowid, "prev_hash": prev_hash, "hash": digest}

    def list_audit(self, payment_id: Optional[str] = None, limit: int = 500) -> list[dict[str, Any]]:
        query = "SELECT * FROM audit"
        params: list[Any] = []
        if payment_id:
            query += " WHERE payment_id = ?"
            params.append(payment_id)
        query += " ORDER BY seq DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._db.execute(query, params).fetchall()
        return [{**dict(row), "data": json.loads(row["data"])} for row in rows]

    def verify_chain(self) -> dict[str, Any]:
        """Recompute every hash. Any edited, removed or reordered entry breaks the chain."""
        with self._lock:
            rows = self._db.execute("SELECT * FROM audit ORDER BY seq ASC").fetchall()
        prev_hash = GENESIS
        for row in rows:
            record = {
                "ts": row["ts"],
                "payment_id": row["payment_id"],
                "event": row["event"],
                "actor": row["actor"],
                "data": json.loads(row["data"]),
            }
            if row["prev_hash"] != prev_hash or row["hash"] != _digest(prev_hash, record):
                return {"ok": False, "count": len(rows), "broken_at": row["seq"]}
            prev_hash = row["hash"]
        return {"ok": True, "count": len(rows), "head": prev_hash}

    # ---- inbox -------------------------------------------------------------

    def replace_inbox(self, items: list[dict[str, Any]]) -> None:
        with self._lock:
            self._db.execute("DELETE FROM inbox")
            for position, item in enumerate(items):
                self._db.execute(
                    "INSERT INTO inbox (id, position, kind, sender, subject, body, status, summary) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'new', '')",
                    (item["id"], position, item["kind"], item["sender"], item["subject"], item["body"]),
                )
            self._db.commit()

    def add_inbox_item(self, item: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            row = self._db.execute("SELECT COALESCE(MAX(position), -1) + 1 AS p FROM inbox").fetchone()
            self._db.execute(
                "INSERT INTO inbox (id, position, kind, sender, subject, body, status, summary) "
                "VALUES (?, ?, ?, ?, ?, ?, 'new', '')",
                (item["id"], row["p"], item["kind"], item["sender"], item["subject"], item["body"]),
            )
            self._db.commit()
        return self.get_inbox_item(item["id"])  # type: ignore[return-value]

    def list_inbox(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM inbox ORDER BY position ASC").fetchall()
        return [dict(row) for row in rows]

    def get_inbox_item(self, item_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._db.execute("SELECT * FROM inbox WHERE id = ?", (item_id,)).fetchone()
        return dict(row) if row else None

    def update_inbox_item(self, item_id: str, **fields: Any) -> None:
        assignments = ", ".join(f"{name} = ?" for name in fields)
        with self._lock:
            self._db.execute(f"UPDATE inbox SET {assignments} WHERE id = ?", [*fields.values(), item_id])
            self._db.commit()

    def recover_interrupted(self) -> None:
        """After a restart, nothing is left half-decided: unfinished reviews go to the owner."""
        with self._lock:
            rows = self._db.execute("SELECT id, status FROM payments WHERE status IN ('reviewing', 'executing')").fetchall()
        for row in rows:
            if row["status"] == "reviewing":
                self.update_payment(row["id"], status="pending_approval", decision="needs_owner", note="Review was interrupted by a restart, so this is held for the owner.")
            else:
                self.update_payment(row["id"], status="failed", note="Interrupted while sending to PayPal. Retrying is safe: the same request id is reused.")
            self.audit("recovered_after_restart", "system", {"was": row["status"]}, row["id"])

    # ---- demo reset --------------------------------------------------------

    def clear_payments(self) -> None:
        """Start a fresh ledger. The reset itself is the first entry of the new chain."""
        with self._lock:
            self._db.execute("DELETE FROM payments")
            self._db.execute("DELETE FROM audit")
            self._db.commit()
        self.audit("ledger_reset", "owner", {"reason": "demo data cleared"})
