"""The PayPal and model clients, exercised against in-memory servers that answer like the real ones."""

import base64
import json

import httpx
from conftest import run

from countersign import llm as llm_module
from countersign.llm import LLM, extract_json
from countersign.paypal import PayPalClient, PayPalError

BASE = "https://api-m.sandbox.paypal.com"
PAYMENT = {"id": "cs_test1", "counterparty_email": "p@example.com", "counterparty_name": "Harbor & Pine Cafe", "amount_cents": 24000,
           "currency": "USD", "purpose": "Homepage illustrations", "items": []}


class FakePayPal:
    def __init__(self):
        self.requests = []
        self.token_calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content and request.headers.get("content-type", "").startswith("application/json") else None
        self.requests.append((request.method, path, body, dict(request.headers)))
        if path == "/v1/oauth2/token":
            self.token_calls += 1
            assert request.headers["authorization"] == "Basic " + base64.b64encode(b"id:secret").decode()
            assert request.content == b"grant_type=client_credentials"
            return httpx.Response(200, json={"access_token": "A21", "expires_in": 32400})
        assert request.headers["authorization"] == "Bearer A21"
        if path == "/v1/payments/payouts" and request.method == "POST":
            if body["items"][0]["amount"]["value"] == "999.00":
                return httpx.Response(422, headers={"paypal-debug-id": "dbg42"}, json={"name": "INSUFFICIENT_FUNDS", "message": "Sender does not have sufficient funds."})
            return httpx.Response(201, json={"batch_header": {"payout_batch_id": "BATCH1", "batch_status": "PENDING"}})
        if path == "/v1/payments/payouts/BATCH1":
            return httpx.Response(200, json={"batch_header": {"payout_batch_id": "BATCH1", "batch_status": "SUCCESS"}, "items": [{"payout_item_id": "ITEM1", "transaction_id": "TX1", "transaction_status": "SUCCESS"}]})
        if path == "/v2/invoicing/generate-next-invoice-number":
            return httpx.Response(200, json={"invoice_number": "0007"})
        if path == "/v2/invoicing/invoices" and request.method == "POST":
            return httpx.Response(201, json={"rel": "self", "href": f"{BASE}/v2/invoicing/invoices/INV2-ABCD", "method": "GET"})
        if path == "/v2/invoicing/invoices/INV2-ABCD/send":
            return httpx.Response(202, json={"rel": "self", "href": "x", "method": "GET"})
        if path == "/v2/invoicing/invoices/INV2-ABCD":
            return httpx.Response(200, json={"id": "INV2-ABCD", "status": "SENT", "detail": {"invoice_number": "0007", "metadata": {"recipient_view_url": "https://www.sandbox.paypal.com/invoice/p/#ABCD"}}})
        if path == "/v2/checkout/orders" and request.method == "POST":
            return httpx.Response(200, json={"id": "ORDER1", "status": "PAYER_ACTION_REQUIRED", "links": [{"rel": "self", "href": "x"}, {"rel": "payer-action", "href": "https://www.sandbox.paypal.com/checkoutnow?token=ORDER1"}]})
        if path == "/v2/checkout/orders/ORDER1/capture":
            return httpx.Response(201, json={"id": "ORDER1", "status": "COMPLETED", "purchase_units": [{"payments": {"captures": [{"id": "CAP1", "status": "COMPLETED"}]}}]})
        return httpx.Response(404, json={"name": "RESOURCE_NOT_FOUND", "message": "nope"})


def client(fake):
    return PayPalClient(BASE, "id", "secret", "Kestrel Studio", transport=httpx.MockTransport(fake))


def test_payout_request_shape_and_token_reuse():
    fake = FakePayPal()
    paypal = client(fake)
    result = run(paypal.create_payout(PAYMENT))
    assert result == {"api": "Payouts", "id": "BATCH1", "status": "PENDING"}
    method, path, body, headers = fake.requests[-1]
    assert headers["paypal-request-id"] == "cs_test1"
    assert body["sender_batch_header"]["sender_batch_id"] == "cs_test1"
    assert body["items"][0] == {"recipient_type": "EMAIL", "receiver": "p@example.com", "amount": {"value": "240.00", "currency": "USD"},
                                "note": "Homepage illustrations", "sender_item_id": "cs_test1"}
    status = run(paypal.get_payout("BATCH1"))
    assert status["status"] == "SUCCESS" and status["transaction_id"] == "TX1"
    assert fake.token_calls == 1


def test_paypal_error_carries_name_and_debug_id():
    paypal = client(FakePayPal())
    try:
        run(paypal.create_payout({**PAYMENT, "amount_cents": 99900}))
    except PayPalError as exc:
        assert (exc.status, exc.name, exc.debug_id) == (422, "INSUFFICIENT_FUNDS", "dbg42")
    else:
        raise AssertionError("expected PayPalError")


def test_invoice_is_created_then_sent():
    fake = FakePayPal()
    payment = {**PAYMENT, "amount_cents": 180000, "items": [{"name": "Workshop", "amount": 600, "quantity": 1}, {"name": "Identity", "amount": 1200, "quantity": 1}]}
    result = run(client(fake).create_and_send_invoice(payment))
    assert result["id"] == "INV2-ABCD" and result["status"] == "SENT" and result["payer_url"].startswith("https://www.sandbox.paypal.com")
    paths = [r[1] for r in fake.requests if r[1] != "/v1/oauth2/token"]
    assert paths == ["/v2/invoicing/generate-next-invoice-number", "/v2/invoicing/invoices", "/v2/invoicing/invoices/INV2-ABCD/send", "/v2/invoicing/invoices/INV2-ABCD"]
    body = fake.requests[2][2]
    assert body["detail"]["invoice_number"] == "0007" and body["detail"]["currency_code"] == "USD"
    assert body["primary_recipients"][0]["billing_info"] == {"email_address": "p@example.com", "business_name": "Harbor & Pine Cafe"}
    assert [i["unit_amount"]["value"] for i in body["items"]] == ["600.00", "1200.00"]


def test_order_create_and_capture():
    fake = FakePayPal()
    paypal = client(fake)
    order = run(paypal.create_order({**PAYMENT, "amount_cents": 4900}, "http://localhost:8000/paypal/return?payment_id=cs_test1", "http://localhost:8000/paypal/cancel?payment_id=cs_test1"))
    assert order["id"] == "ORDER1" and "token=ORDER1" in order["approve_url"]
    body = fake.requests[-1][2]
    assert body["intent"] == "CAPTURE" and body["purchase_units"][0]["amount"] == {"currency_code": "USD", "value": "49.00"}
    assert body["purchase_units"][0]["payee"] == {"email_address": "p@example.com"}
    captured = run(paypal.capture_order("ORDER1", "cs_test1"))
    assert captured["status"] == "COMPLETED" and captured["capture_id"] == "CAP1"
    assert fake.requests[-1][3]["paypal-request-id"] == "cs_test1-capture"


def test_unconfigured_client_fails_clearly():
    paypal = PayPalClient(BASE, "", "")
    try:
        run(paypal.ping())
    except PayPalError as exc:
        assert exc.name == "NOT_CONFIGURED"
    else:
        raise AssertionError("expected PayPalError")


def test_extract_json_tolerates_fences_and_prose():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Sure! Here it is: {"risk_score": 12, "signals": ["x {y}"]} hope it helps') == {"risk_score": 12, "signals": ["x {y}"]}


def test_llm_retries_rate_limits_and_falls_back_from_json_mode(monkeypatch):
    monkeypatch.setattr(llm_module, "RETRY_WAITS", (0.0, 0.0))
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if len(seen) == 1:
            return httpx.Response(429, json={"error": "slow down"})
        if "response_format" in body:
            return httpx.Response(400, json={"error": "response_format not supported"})
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": 'ok {"risk_score": 5}'}}]})

    model = LLM("https://llm.example/v1", "key", "m", transport=httpx.MockTransport(handler))
    assert run(model.json("sys", "user")) == {"risk_score": 5}
    assert len(seen) == 3
