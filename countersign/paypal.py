"""PayPal REST client (sandbox): OAuth, Payouts, Invoicing v2 and Orders v2.

Every write carries a PayPal-Request-Id derived from the Countersign payment id,
so a retry by a confused agent or a flaky network can never pay twice.
"""

from __future__ import annotations

import time
from typing import Any, Optional

import httpx

from .models import money


class PayPalError(Exception):
    def __init__(self, status: int, name: str, message: str, debug_id: str = "", details: Any = None) -> None:
        super().__init__(f"{name}: {message}")
        self.status = status
        self.name = name
        self.message = message
        self.debug_id = debug_id
        self.details = details

    def as_dict(self) -> dict[str, Any]:
        return {
            "http_status": self.status,
            "name": self.name,
            "message": self.message,
            "debug_id": self.debug_id,
            "details": self.details,
        }


def _link(body: dict[str, Any], *rels: str) -> str:
    for link in body.get("links") or []:
        if link.get("rel") in rels:
            return link.get("href", "")
    return ""


def _with_proof(text: str, payment: dict[str, Any]) -> str:
    """Put the link to the signed proof inside the PayPal payment, where the recipient will see it."""
    url = payment.get("verify_url")
    return f"{text}\n\nAuthorised by Countersign. Verify: {url}" if url else text


class PayPalClient:
    def __init__(
        self,
        base_url: str,
        client_id: str,
        client_secret: str,
        brand_name: str = "Countersign",
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.brand_name = brand_name
        self._http = httpx.AsyncClient(timeout=30.0, transport=transport)
        self._token = ""
        self._token_expiry = 0.0

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    async def close(self) -> None:
        await self._http.aclose()

    # ---- plumbing ----------------------------------------------------------

    async def token(self) -> str:
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        if not self.configured:
            raise PayPalError(0, "NOT_CONFIGURED", "Set PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET in .env.")
        try:
            response = await self._http.post(
                f"{self.base_url}/v1/oauth2/token",
                auth=(self.client_id, self.client_secret),
                data={"grant_type": "client_credentials"},
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise PayPalError(0, "NETWORK_ERROR", f"Could not reach PayPal: {exc.__class__.__name__}") from exc
        if response.status_code != 200:
            raise self._error(response)
        body = response.json()
        self._token = body["access_token"]
        self._token_expiry = time.time() + float(body.get("expires_in", 3600))
        return self._token

    @staticmethod
    def _error(response: httpx.Response) -> PayPalError:
        debug_id = response.headers.get("paypal-debug-id", "")
        try:
            body = response.json()
        except ValueError:
            body = {}
        name = body.get("name") or body.get("error") or f"HTTP_{response.status_code}"
        message = body.get("message") or body.get("error_description") or response.text[:300] or "Request failed"
        details = body.get("details")
        if isinstance(details, list) and details:
            first = details[0]
            issue = first.get("issue") or first.get("field") or ""
            description = first.get("description") or ""
            if issue or description:
                message = f"{message} ({issue}: {description})".replace("(: ", "(")
        return PayPalError(response.status_code, name, message, debug_id or body.get("debug_id", ""), details)

    async def _request(
        self,
        method: str,
        path: str,
        json: Optional[dict[str, Any]] = None,
        request_id: Optional[str] = None,
        prefer_representation: bool = False,
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {await self.token()}", "Content-Type": "application/json"}
        if request_id:
            headers["PayPal-Request-Id"] = request_id
        if prefer_representation:
            headers["Prefer"] = "return=representation"
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        try:
            response = await self._http.request(method, url, headers=headers, json=json)
        except httpx.HTTPError as exc:
            raise PayPalError(0, "NETWORK_ERROR", f"Could not reach PayPal: {exc.__class__.__name__}") from exc
        if response.status_code >= 400:
            raise self._error(response)
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            return {}

    async def ping(self) -> None:
        await self.token()

    # ---- Payouts: pay a person --------------------------------------------

    async def create_payout(self, payment: dict[str, Any]) -> dict[str, Any]:
        body = {
            "sender_batch_header": {
                "sender_batch_id": payment["id"],
                "email_subject": f"You have a payment from {self.brand_name}",
                "email_message": payment["purpose"][:1000] or "Payment",
            },
            "items": [
                {
                    "recipient_type": "EMAIL",
                    "receiver": payment["counterparty_email"],
                    "amount": {"value": money(payment["amount_cents"]), "currency": payment["currency"]},
                    "note": _with_proof(payment["purpose"][:3800] or "Payment", payment),
                    "sender_item_id": payment["id"],
                }
            ],
        }
        result = await self._request("POST", "/v1/payments/payouts", json=body, request_id=payment["id"])
        header = result.get("batch_header") or {}
        return {
            "api": "Payouts",
            "id": header.get("payout_batch_id", ""),
            "status": header.get("batch_status", "PENDING"),
        }

    async def get_payout(self, batch_id: str) -> dict[str, Any]:
        result = await self._request("GET", f"/v1/payments/payouts/{batch_id}")
        header = result.get("batch_header") or {}
        item = (result.get("items") or [{}])[0]
        return {
            "api": "Payouts",
            "id": header.get("payout_batch_id", batch_id),
            "status": header.get("batch_status", ""),
            "item_status": item.get("transaction_status", ""),
            "transaction_id": item.get("transaction_id", ""),
            "payout_item_id": item.get("payout_item_id", ""),
        }

    # ---- Invoicing: bill a client -----------------------------------------

    async def create_and_send_invoice(self, payment: dict[str, Any]) -> dict[str, Any]:
        number = (await self._request("POST", "/v2/invoicing/generate-next-invoice-number")).get("invoice_number")
        items = payment.get("items") or []
        if items:
            lines = [
                {
                    "name": str(item["name"])[:200],
                    "quantity": str(item.get("quantity", 1)),
                    "unit_amount": {"currency_code": payment["currency"], "value": f"{float(item['amount']):.2f}"},
                }
                for item in items
            ]
        else:
            lines = [
                {
                    "name": (payment["purpose"] or "Services")[:200],
                    "quantity": "1",
                    "unit_amount": {"currency_code": payment["currency"], "value": money(payment["amount_cents"])},
                }
            ]
        billing: dict[str, Any] = {"email_address": payment["counterparty_email"]}
        if payment.get("counterparty_name"):
            billing["business_name"] = payment["counterparty_name"][:300]
        detail: dict[str, Any] = {
            "currency_code": payment["currency"],
            "reference": payment["id"],
            "note": _with_proof((payment["purpose"] or "Thank you for your business.")[:3800], payment),
            "payment_term": {"term_type": "NET_10"},
        }
        if number:
            detail["invoice_number"] = number
        body = {"detail": detail, "primary_recipients": [{"billing_info": billing}], "items": lines}

        created = await self._request(
            "POST", "/v2/invoicing/invoices", json=body, request_id=payment["id"], prefer_representation=True
        )
        invoice_id = created.get("id") or (created.get("href") or "").rstrip("/").split("/")[-1]
        if not invoice_id:
            raise PayPalError(0, "UNEXPECTED_RESPONSE", "PayPal created the invoice but did not return its id.")

        await self._request(
            "POST",
            f"/v2/invoicing/invoices/{invoice_id}/send",
            json={"send_to_invoicer": False, "send_to_recipient": True},
            request_id=payment["id"] + "-send",
        )
        return await self.get_invoice(invoice_id)

    async def get_invoice(self, invoice_id: str) -> dict[str, Any]:
        result = await self._request("GET", f"/v2/invoicing/invoices/{invoice_id}")
        detail = result.get("detail") or {}
        metadata = detail.get("metadata") or {}
        return {
            "api": "Invoicing",
            "id": result.get("id", invoice_id),
            "status": result.get("status", ""),
            "invoice_number": detail.get("invoice_number", ""),
            "payer_url": metadata.get("recipient_view_url", ""),
        }

    # ---- Orders: buy from a vendor, owner approves inside PayPal -----------

    async def create_order(self, payment: dict[str, Any], return_url: str, cancel_url: str) -> dict[str, Any]:
        body = {
            "intent": "CAPTURE",
            "purchase_units": [
                {
                    "reference_id": payment["id"],
                    "custom_id": payment["id"],
                    "description": (payment["purpose"] or "Purchase")[:127],
                    "amount": {"currency_code": payment["currency"], "value": money(payment["amount_cents"])},
                    "payee": {"email_address": payment["counterparty_email"]},
                }
            ],
            "payment_source": {
                "paypal": {
                    "experience_context": {
                        "brand_name": self.brand_name[:127],
                        "user_action": "PAY_NOW",
                        "shipping_preference": "NO_SHIPPING",
                        "return_url": return_url,
                        "cancel_url": cancel_url,
                    }
                }
            },
        }
        result = await self._request("POST", "/v2/checkout/orders", json=body, request_id=payment["id"])
        return {
            "api": "Orders",
            "id": result.get("id", ""),
            "status": result.get("status", ""),
            "approve_url": _link(result, "payer-action", "approve"),
        }

    async def capture_order(self, order_id: str, request_id: str) -> dict[str, Any]:
        result = await self._request(
            "POST", f"/v2/checkout/orders/{order_id}/capture", json={}, request_id=request_id + "-capture"
        )
        return self._order_summary(result, order_id)

    async def get_order(self, order_id: str) -> dict[str, Any]:
        return self._order_summary(await self._request("GET", f"/v2/checkout/orders/{order_id}"), order_id)

    # ---- Balance -------------------------------------------------------------

    async def get_balance(self, currency: str = "USD") -> int:
        """Available balance in cents, from PayPal's reporting API."""
        result = await self._request("GET", f"/v1/reporting/balances?currency_code={currency}")
        for entry in result.get("balances") or []:
            if entry.get("currency") == currency:
                value = (entry.get("available_balance") or entry.get("total_balance") or {}).get("value", "0")
                return int(round(float(value) * 100))
        raise PayPalError(0, "NO_BALANCE", f"PayPal returned no {currency} balance.")

    # ---- Reversals: the owner takes a payment back --------------------------

    async def cancel_payout_item(self, item_id: str) -> dict[str, Any]:
        """Recall a payout. PayPal only allows this while the recipient has not claimed it yet."""
        result = await self._request("POST", f"/v1/payments/payouts-item/{item_id}/cancel")
        return {"api": "Payouts", "action": "recalled", "item_status": result.get("transaction_status", "")}

    async def refund_capture(self, capture_id: str, request_id: str) -> dict[str, Any]:
        result = await self._request("POST", f"/v2/payments/captures/{capture_id}/refund", json={}, request_id=request_id + "-refund")
        return {"api": "Payments", "action": "refunded", "refund_id": result.get("id", ""), "refund_status": result.get("status", "")}

    async def cancel_invoice(self, invoice_id: str) -> dict[str, Any]:
        await self._request(
            "POST", f"/v2/invoicing/invoices/{invoice_id}/cancel",
            json={"send_to_recipient": True, "send_to_invoicer": False, "note": "Cancelled by the business owner."},
        )
        return {"api": "Invoicing", "action": "cancelled"}

    async def remind_invoice(self, invoice_id: str, note: str) -> dict[str, Any]:
        await self._request(
            "POST", f"/v2/invoicing/invoices/{invoice_id}/remind",
            json={"subject": f"Reminder: invoice from {self.brand_name}", "note": note[:4000],
                  "send_to_invoicer": False, "send_to_recipient": True},
        )
        return {"api": "Invoicing", "action": "reminded"}

    # ---- Webhooks: PayPal tells us what happened next ------------------------

    async def verify_webhook(self, headers: dict[str, str], event: dict[str, Any], webhook_id: str) -> bool:
        """Ask PayPal whether this webhook really came from PayPal (signature check on PayPal's side)."""
        h = {key.lower(): value for key, value in headers.items()}
        body = {
            "auth_algo": h.get("paypal-auth-algo", ""),
            "cert_url": h.get("paypal-cert-url", ""),
            "transmission_id": h.get("paypal-transmission-id", ""),
            "transmission_sig": h.get("paypal-transmission-sig", ""),
            "transmission_time": h.get("paypal-transmission-time", ""),
            "webhook_id": webhook_id,
            "webhook_event": event,
        }
        if not all(body[k] for k in ("auth_algo", "cert_url", "transmission_id", "transmission_sig", "transmission_time")):
            return False
        result = await self._request("POST", "/v1/notifications/verify-webhook-signature", json=body)
        return result.get("verification_status") == "SUCCESS"

    @staticmethod
    def _order_summary(result: dict[str, Any], order_id: str) -> dict[str, Any]:
        capture_id = ""
        for unit in result.get("purchase_units") or []:
            captures = (unit.get("payments") or {}).get("captures") or []
            if captures:
                capture_id = captures[0].get("id", "")
        return {
            "api": "Orders",
            "id": result.get("id", order_id),
            "status": result.get("status", ""),
            "capture_id": capture_id,
            "approve_url": _link(result, "payer-action", "approve"),
        }
