"""HTTP API and web console for Countersign."""

from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__
from .agent import Agent
from .compiler import compile_mandate
from .config import Settings, load_settings
from .events import EventBus
from .guard import Guard, GuardError
from .inbox import default_mandate, hijack_intent, seed_items
from .llm import LLM, LLMError
from .models import MONEY_OUT_RAILS, Mandate, PaymentIntent
from .paypal import PayPalClient, PayPalError
from .store import Store

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


class TextBody(BaseModel):
    text: str


class NoteBody(BaseModel):
    note: str = ""


class IntentBody(PaymentIntent):
    actor: str = "external-agent"


def create_app(
    settings: Optional[Settings] = None,
    store: Optional[Store] = None,
    paypal: Any = None,
    llm: Any = None,
) -> FastAPI:
    settings = settings or load_settings()
    store = store or Store(settings.db_path)
    bus = EventBus()

    if settings.offline_preview and (paypal is None or llm is None):
        from .preview import PreviewLLM, PreviewPayPal

        paypal = paypal or PreviewPayPal()
        llm = llm or PreviewLLM()
    paypal = paypal or PayPalClient(settings.paypal_base, settings.paypal_client_id, settings.paypal_client_secret, settings.business_name)
    llm = llm or LLM(settings.llm_base_url, settings.llm_api_key, settings.llm_model)

    guard = Guard(store, paypal, llm, bus, settings.public_base_url)
    agent = Agent(store, guard, llm, bus, settings.business_name)
    background: set[asyncio.Task] = set()
    health: dict[str, Any] = {}
    run_lock = asyncio.Lock()

    def spawn(coroutine) -> None:
        task = asyncio.create_task(coroutine)
        background.add(task)
        task.add_done_callback(background.discard)

    def seed(force: bool = False) -> None:
        if force or store.get_mandate() is None:
            store.save_mandate(default_mandate(settings), actor="system")
        if force or not store.list_inbox():
            store.replace_inbox(seed_items(settings))

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        store.recover_interrupted()
        seed()
        yield
        for task in list(background):
            task.cancel()
        await paypal.close()
        await llm.close()

    app = FastAPI(title="Countersign", version=__version__, lifespan=lifespan)
    app.state.store, app.state.guard, app.state.agent, app.state.bus = store, guard, agent, bus

    # ---- console -----------------------------------------------------------

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-store"})

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    # ---- status ------------------------------------------------------------

    @app.get("/api/status")
    async def status(deep: bool = False) -> dict[str, Any]:
        paypal_state: dict[str, Any] = {"ok": False, "detail": "Add PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET to .env"}
        if paypal.configured:
            try:
                await paypal.ping()
                paypal_state = {"ok": True, "detail": "Connected to the PayPal sandbox"}
            except PayPalError as exc:
                paypal_state = {"ok": False, "detail": f"{exc.name}: {exc.message}"}
        llm_state: dict[str, Any] = {"ok": llm.configured, "model": llm.model, "detail": "Ready" if llm.configured else "Add LLM_API_KEY to .env"}
        if llm.configured and (deep or "llm" not in health):
            # One real round trip, so a wrong key or model name shows up here and not mid-payment.
            try:
                await llm.ping()
                health["llm"] = {"ok": True, "model": llm.model, "detail": "Answered a test prompt"}
            except LLMError as exc:
                health["llm"] = {"ok": False, "model": llm.model, "detail": str(exc)}
        llm_state = health.get("llm", llm_state)
        return {
            "version": __version__,
            "business": settings.business_name,
            "preview": settings.offline_preview,
            "paypal": paypal_state,
            "llm": llm_state,
            "chain": store.verify_chain(),
        }

    @app.get("/api/stats")
    async def stats() -> dict[str, Any]:
        mandate = store.get_mandate()
        payments = store.list_payments()
        week_ago = datetime.now(timezone.utc) - timedelta(days=7)
        stopped = [p for p in payments if p["status"] in ("blocked", "rejected") and p["rail"] in MONEY_OUT_RAILS]
        return {
            "week_committed": store.committed_cents(week_ago) / 100,
            "weekly_budget": mandate.weekly_budget if mandate else None,
            "currency": mandate.currency if mandate else "USD",
            "sent": sum(1 for p in payments if p["status"] == "executed"),
            "waiting": sum(1 for p in payments if p["status"] in ("pending_approval", "awaiting_payer")),
            "stopped": len([p for p in payments if p["status"] in ("blocked", "rejected")]),
            "stopped_amount": sum(p["amount_cents"] for p in stopped) / 100,
        }

    # ---- mandate -----------------------------------------------------------

    @app.get("/api/mandate")
    async def get_mandate() -> Mandate:
        mandate = store.get_mandate()
        if not mandate:
            raise HTTPException(404, "No mandate has been set.")
        return mandate

    @app.put("/api/mandate")
    async def put_mandate(mandate: Mandate) -> Mandate:
        if mandate.auto_approve_limit > mandate.per_payment_cap:
            mandate.auto_approve_limit = mandate.per_payment_cap
        saved = store.save_mandate(mandate)
        bus.publish("mandate", saved.model_dump(mode="json"))
        return saved

    @app.post("/api/mandate/compile")
    async def compile_and_activate(body: TextBody) -> Mandate:
        try:
            mandate = await compile_mandate(llm, body.text)
        except LLMError as exc:
            raise HTTPException(502, str(exc)) from exc
        saved = store.save_mandate(mandate)
        bus.publish("mandate", saved.model_dump(mode="json"))
        return saved

    # ---- inbox and agent ---------------------------------------------------

    @app.get("/api/inbox")
    async def inbox() -> list[dict[str, Any]]:
        return store.list_inbox()

    async def run_items(item_ids: list[str]) -> None:
        async with run_lock:
            for item_id in item_ids:
                await agent.run_item(item_id)

    @app.post("/api/inbox/{item_id}/run", status_code=202)
    async def run_one(item_id: str) -> dict[str, Any]:
        if not store.get_inbox_item(item_id):
            raise HTTPException(404, "No such work item.")
        store.update_inbox_item(item_id, status="queued")
        bus.publish("inbox", {"id": item_id, "status": "queued"})
        spawn(run_items([item_id]))
        return {"queued": [item_id]}

    @app.post("/api/inbox/run-all", status_code=202)
    async def run_all() -> dict[str, Any]:
        item_ids = [item["id"] for item in store.list_inbox() if item["status"] in ("new", "error")]
        for item_id in item_ids:
            store.update_inbox_item(item_id, status="queued")
            bus.publish("inbox", {"id": item_id, "status": "queued"})
        spawn(run_items(item_ids))
        return {"queued": item_ids}

    @app.post("/api/command", status_code=202)
    async def command(body: TextBody) -> dict[str, Any]:
        text = body.text.strip()
        if not text:
            raise HTTPException(400, "Type an instruction for the agent first.")
        item = store.add_inbox_item(
            {"id": "owner-" + uuid.uuid4().hex[:6], "kind": "owner", "sender": "You (owner)", "subject": text[:90], "body": text}
        )
        store.update_inbox_item(item["id"], status="queued")
        bus.publish("inbox", {"id": item["id"], "status": "queued", "added": True})
        spawn(run_items([item["id"]]))
        return {"queued": [item["id"]]}

    # ---- payments ----------------------------------------------------------

    @app.post("/api/intents")
    async def submit_intent(body: IntentBody) -> dict[str, Any]:
        """The door for any agent, in any framework: ask here, and Countersign decides."""
        intent = PaymentIntent.model_validate(body.model_dump(exclude={"actor"}))
        return await guard.submit(intent, actor=body.actor[:60] or "external-agent")

    @app.post("/api/redteam")
    async def redteam() -> dict[str, Any]:
        """Skip the agent entirely and send what a hijacked one would. The guard should hold."""
        intent = PaymentIntent.model_validate(hijack_intent(settings))
        return await guard.submit(intent, actor="Hijacked agent (simulated)")

    @app.get("/api/payments")
    async def payments() -> list[dict[str, Any]]:
        return store.list_payments()

    @app.get("/api/payments/{payment_id}")
    async def payment(payment_id: str) -> dict[str, Any]:
        record = store.get_payment(payment_id)
        if not record:
            raise HTTPException(404, "No such payment.")
        return {**record, "audit": list(reversed(store.list_audit(payment_id)))}

    async def owner_action(action, payment_id: str, *args: Any) -> dict[str, Any]:
        try:
            return await action(payment_id, *args)
        except GuardError as exc:
            raise HTTPException(409, str(exc)) from exc
        except PayPalError as exc:
            raise HTTPException(502, f"PayPal: {exc.message}") from exc

    @app.post("/api/payments/{payment_id}/approve")
    async def approve(payment_id: str, body: NoteBody = NoteBody()) -> dict[str, Any]:
        return await owner_action(guard.approve, payment_id, body.note)

    @app.post("/api/payments/{payment_id}/reject")
    async def reject(payment_id: str, body: NoteBody = NoteBody()) -> dict[str, Any]:
        return await owner_action(guard.reject, payment_id, body.note)

    @app.post("/api/payments/{payment_id}/retry")
    async def retry(payment_id: str) -> dict[str, Any]:
        return await owner_action(guard.retry, payment_id)

    @app.post("/api/payments/{payment_id}/sync")
    async def sync(payment_id: str) -> dict[str, Any]:
        return await owner_action(guard.sync, payment_id)

    @app.get("/paypal/return", include_in_schema=False)
    async def paypal_return(payment_id: str, token: str = "") -> RedirectResponse:
        try:
            await guard.capture_return(payment_id, token)
        except GuardError:
            pass
        return RedirectResponse(f"/?payment={payment_id}", status_code=303)

    @app.get("/paypal/cancel", include_in_schema=False)
    async def paypal_cancel(payment_id: str) -> RedirectResponse:
        await guard.cancel_return(payment_id)
        return RedirectResponse(f"/?payment={payment_id}", status_code=303)

    # ---- audit -------------------------------------------------------------

    @app.get("/api/audit")
    async def audit(limit: int = 500) -> list[dict[str, Any]]:
        return store.list_audit(limit=max(1, min(limit, 2000)))

    @app.get("/api/audit/verify")
    async def verify() -> dict[str, Any]:
        return store.verify_chain()

    # ---- demo --------------------------------------------------------------

    @app.post("/api/demo/reset")
    async def reset() -> dict[str, Any]:
        store.clear_payments()
        seed(force=True)
        bus.publish("reset", {})
        return {"ok": True}

    # ---- live stream -------------------------------------------------------

    @app.get("/api/events", include_in_schema=False)
    async def events(request: Request) -> StreamingResponse:
        queue = bus.subscribe()

        async def stream():
            try:
                yield "retry: 2000\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15)
                        yield f"event: {event['kind']}\ndata: {json.dumps(event['data'])}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"
            finally:
                bus.unsubscribe(queue)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app

