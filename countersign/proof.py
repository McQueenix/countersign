"""Countersign Proof: a signed, portable receipt for every decision about an agent's payment.

When a payment is sent, blocked or refused, Countersign issues a small JSON document that says
what was asked, under which mandate, who signed, and what PayPal returned, and signs it with an
Ed25519 key. The link to it travels inside the PayPal payment itself (the payout note or the
invoice note), so the person who receives the money, PayPal, or an auditor can check that an
agent payment was authorised, without trusting the agent or this server's database.

Change one character of a proof and the signature no longer verifies.
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from .models import Mandate, money
from .store import Store, now_iso

PROOF_TYPE = "countersign.proof"
FINAL_STATUSES = ("executed", "blocked", "rejected")


def canonical(value: Any) -> bytes:
    """One exact byte form for a JSON value, so the same contents always give the same signature."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def unb64u(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def mandate_fingerprint(mandate: Optional[Mandate]) -> str:
    """SHA-256 of the mandate as it stood, so a proof names the exact rules a payment was judged by."""
    if mandate is None:
        return ""
    return hashlib.sha256(canonical(mandate.model_dump(mode="json"))).hexdigest()


def _worst(statuses: list[str]) -> str:
    if "deny" in statuses:
        return "refused"
    if "escalate" in statuses:
        return "needs owner"
    return "within limits"


class Notary:
    """Holds this Countersign's signing key and issues and checks proofs."""

    def __init__(self, store: Store, issuer: str) -> None:
        self.store = store
        self.issuer = issuer
        stored = store.get_kv("signing_key")
        if stored:
            self._key = Ed25519PrivateKey.from_private_bytes(unb64u(stored))
        else:
            self._key = Ed25519PrivateKey.generate()
            raw = self._key.private_bytes(
                serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
            )
            store.set_kv("signing_key", b64u(raw))
        public = self._key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.public_key = b64u(public)
        self.key_id = hashlib.sha256(public).hexdigest()[:16]

    def public_info(self) -> dict[str, str]:
        return {"issuer": self.issuer, "alg": "Ed25519", "key_id": self.key_id, "public_key": self.public_key}

    def claims(self, payment: dict[str, Any], audit_head: str) -> dict[str, Any]:
        policy_statuses = [c["status"] for c in payment["checks"] if c.get("source") == "policy"]
        signatures: list[dict[str, Any]] = [
            {"role": "requester", "by": payment["actor"]},
            {"role": "policy", "result": _worst(policy_statuses)},
        ]
        review = payment.get("review") or {}
        if review.get("status"):
            signatures.append(
                {"role": "ai_review", "result": _worst([review["status"]]), "risk": review.get("risk_score")}
            )
        if payment["decision"] == "owner_approved":
            for name in payment.get("signers") or ["Owner"]:
                signatures.append({"role": "owner", "by": name, "result": "approved"})
        elif payment["decision"] == "owner_rejected":
            signatures.append({"role": "owner", "result": "refused"})
        elif payment["decision"] == "expired":
            signatures.append({"role": "owner", "result": "no answer in time"})
        paypal = payment.get("paypal") or {}
        reference = {key: paypal[key] for key in ("api", "id", "status", "capture_id", "invoice_number") if paypal.get(key)}
        return {
            "v": 1,
            "type": PROOF_TYPE,
            "issuer": self.issuer,
            "key_id": self.key_id,
            "payment_id": payment["id"],
            "rail": payment["rail"],
            "amount": money(payment["amount_cents"]),
            "currency": payment["currency"],
            "payee": payment["counterparty_email"],
            "purpose": payment["purpose"],
            "category": payment.get("category", ""),
            "outcome": payment["status"],
            "decision": payment["decision"],
            "mandate_sha256": payment.get("mandate_hash", ""),
            "signatures": signatures,
            "stopped_by": [c["rule"] for c in payment["checks"] if c["status"] != "pass"],
            "paypal": reference,
            "audit_head": audit_head,
            "requested_at": payment["created_at"],
            "issued_at": now_iso(),
        }

    def sign(self, claims: dict[str, Any]) -> str:
        return b64u(self._key.sign(canonical(claims)))

    def issue(self, payment: dict[str, Any]) -> dict[str, Any]:
        trail = self.store.list_audit(payment["id"], limit=1)
        audit_head = trail[0]["hash"] if trail else ""
        claims = self.claims(payment, audit_head)
        signature = self.sign(claims)
        self.store.save_proof(payment["id"], claims, signature, self.key_id)
        self.store.audit("proof_issued", "countersign", {"key_id": self.key_id, "signature": signature[:16]}, payment["id"])
        return {"proof": claims, "signature": signature, **self.public_info()}

    def verify(self, claims: dict[str, Any], signature: str, public_key: Optional[str] = None) -> bool:
        try:
            key = Ed25519PublicKey.from_public_bytes(unb64u(public_key or self.public_key))
            key.verify(unb64u(signature), canonical(claims))
            return True
        except (InvalidSignature, ValueError, TypeError):
            return False
