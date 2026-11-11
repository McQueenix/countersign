"""HTTP API and web console for Countersign."""

from __future__ import annotations

import asyncio
import json
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import segno
from pydantic import BaseModel

from . import __version__, ask, drill, oversight, planner, policy
from .agent import Agent
from .agents import AgentRegistry
from .compiler import compile_mandate
from .config import Settings, load_settings
from .events import EventBus
from .guard import Guard, GuardError
from .inbox import default_mandate, hijack_intent, seed_items
from .llm import LLM, LLMError
from .models import MONEY_OUT_RAILS, Mandate, PaymentIntent
from .paypal import PayPalClient, PayPalError
from .proof import Notary, canonical, mandate_fingerprint
from .store import COMMITTED, Store

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


class TextBody(BaseModel):
    text: str


class NoteBody(BaseModel):
    note: str = ""
    signer: str = "Owner"
    remember_payee: bool = False
    after_invoice: Optional[str] = None


class AgentBody(BaseModel):
    name: str
    weekly_allowance: Optional[float] = None


class StandingBody(BaseModel):
    name: str = ""
    email: str
    amount: float
    every_days: int = 7
    purpose: str = ""


class PlanBody(BaseModel):
    scenario: Optional[str] = None
    risk_appetite: Optional[str] = None


class ApplyBody(BaseModel):
    signer: str = "Owner"


class DocBody(BaseModel):
    name: str
    text: str


class PassportBody(BaseModel):
    passport: dict[str, Any]
    signature: str


class BoostBody(BaseModel):
    limit: float
    minutes: int = 60


class EmailBody(BaseModel):
    body: str
    sender: str = ""
    subject: str = ""


class ActionBody(BaseModel):
    action: str


class ProofBody(BaseModel):
    proof: dict[str, Any]
    signature: str


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
    llm = llm or LLM(settings.llm_base_url, settings.llm_api_key, settings.llm_model,
                     reasoning_effort=settings.llm_reasoning_effort or None)

    notary = Notary(store, settings.business_name)
    guard = Guard(store, paypal, llm, bus, settings.public_base_url, notary)
    agent = Agent(store, guard, llm, bus, settings.business_name)
    registry = AgentRegistry(store)
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

    async def standing_loop() -> None:
        """Run standing orders when they fall due. Each run is an ordinary request through the guard."""
        while True:
            for order in guard.standing.due():
                try:
                    await guard.run_standing(order["id"])
                except Exception as exc:  # noqa: BLE001 - one bad order must not stop the others
                    store.audit("standing_order_failed", "countersign", {"order": order["id"], "error": str(exc)[:200]})
            try:
                await guard.expire_holds()
                await guard.remind_due()
                await guard.watch_invoices()
            except Exception as exc:  # noqa: BLE001 - the loop must keep running
                store.audit("expiry_failed", "countersign", {"error": str(exc)[:200]})
            await asyncio.sleep(20)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        store.recover_interrupted()
        seed()
        guard.resume_scheduled()
        spawn(standing_loop())
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
        from .config import ENV_FILE, ROOT
        where = f"read from {ENV_FILE}" if ENV_FILE else f"no .env file found in {ROOT}"
        paypal_state: dict[str, Any] = {"ok": False, "detail": f"Add PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET to .env ({where})"}
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
        balance = await guard.balance() if paypal.configured else None
        return {
            "version": __version__,
            "business": settings.business_name,
            "preview": settings.offline_preview,
            "paypal": paypal_state,
            "llm": llm_state,
            "chain": store.verify_chain(),
            "balance": balance / 100 if balance is not None else None,
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
            "forecast": _forecast(mandate, payments),
            "cash": await _cash(payments),
            "categories": _category_use(mandate),
        }

    def _forecast(mandate: Optional[Mandate], payments: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
        """When the weekly budget runs out at the pace of the last seven days."""
        if not mandate or mandate.weekly_budget is None:
            return None
        now = datetime.now(timezone.utc)
        week_ago = now - timedelta(days=7)
        used = store.committed_cents(week_ago)
        limit = int(round(mandate.weekly_budget * 100))
        times = [datetime.fromisoformat(p["created_at"]) for p in payments
                 if p["rail"] in MONEY_OUT_RAILS and p["status"] in COMMITTED and datetime.fromisoformat(p["created_at"]) >= week_ago]
        if not used or not times:
            return {"used": used / 100, "limit": limit / 100, "days_left": None, "text": "Nothing spent this week yet."}
        elapsed_days = max((now - min(times)).total_seconds() / 86400, 1.0)
        per_day = used / elapsed_days
        left = max(limit - used, 0)
        days_left = left / per_day if per_day else None
        if left == 0:
            text = "The weekly budget is used up. New payments wait until older ones fall out of the 7-day window."
        elif days_left is not None and days_left < 7:
            text = f"At this pace (${per_day / 100:,.2f} a day) the weekly budget runs out in about {days_left:.1f} days."
        else:
            text = f"At this pace (${per_day / 100:,.2f} a day) the weekly budget lasts the week."
        return {"used": used / 100, "limit": limit / 100, "per_day": per_day / 100,
                "days_left": round(days_left, 1) if days_left is not None else None, "text": text}

    async def _cash(payments: list[dict[str, Any]]) -> dict[str, Any]:
        """Where the money stands once everything in flight lands."""
        balance = await guard.balance() if paypal.configured else None
        owed_to_you = sum(p["amount_cents"] for p in payments if p["rail"] == "invoice" and p["status"] == "executed"
                          and (p.get("paypal") or {}).get("status") not in ("PAID", "CANCELLED", "MARKED_AS_PAID"))
        going_out = sum(p["amount_cents"] for p in payments if p["rail"] in MONEY_OUT_RAILS
                        and p["status"] in ("pending_approval", "scheduled", "awaiting_payer", "awaiting_client"))
        after = (balance or 0) + owed_to_you - going_out
        return {"balance": balance / 100 if balance is not None else None, "owed_to_you": owed_to_you / 100,
                "going_out": going_out / 100, "after": after / 100 if balance is not None else None}

    def _category_use(mandate: Optional[Mandate]) -> list[dict[str, Any]]:
        if not mandate:
            return []
        week_ago = datetime.now(timezone.utc) - timedelta(days=7)
        return [{"category": name, "limit": limit, "used": store.committed_by_category(name, week_ago) / 100}
                for name, limit in mandate.category_budgets.items()]

    # ---- mandate -----------------------------------------------------------

    @app.get("/api/mandate")
    async def get_mandate() -> Mandate:
        mandate = store.get_mandate()
        if not mandate:
            raise HTTPException(404, "No mandate has been set.")
        return mandate

    @app.put("/api/mandate")
    async def put_mandate(mandate: Mandate) -> Mandate:
        if mandate.active:
            mandate.paused_reason = ""
        if mandate.auto_approve_limit > mandate.per_payment_cap:
            mandate.auto_approve_limit = mandate.per_payment_cap
        saved = store.save_mandate(mandate)
        bus.publish("mandate", saved.model_dump(mode="json"))
        return saved

    @app.get("/api/mandate/history")
    async def mandate_history() -> list[dict[str, Any]]:
        """Every version of the mandate, newest first. Proofs name versions by their sha256."""
        return store.mandate_history()

    @app.post("/api/mandate/history/{sha}/restore")
    async def restore_mandate(sha: str) -> Mandate:
        match = next((v for v in store.mandate_history() if v["sha256"] == sha), None)
        if not match:
            raise HTTPException(404, "No mandate version with that fingerprint.")
        mandate = Mandate.model_validate(match["mandate"])
        mandate.boost_limit, mandate.boost_until = None, None
        saved = store.save_mandate(mandate, actor="owner")
        store.audit("mandate_restored", "owner", {"from_sha256": sha})
        bus.publish("mandate", saved.model_dump(mode="json"))
        return saved

    @app.post("/api/emergency-stop")
    async def emergency_stop() -> dict[str, Any]:
        try:
            return await guard.freeze_all()
        except GuardError as exc:
            raise HTTPException(409, str(exc)) from exc

    # ---- agent passport -----------------------------------------------------

    def _amount(value: Optional[float]) -> Optional[str]:
        return None if value is None else f"{value:.2f}"

    def _track_record(now: datetime) -> dict[str, Any]:
        """How this agent has behaved over the last 30 days: what a merchant would want to know before trusting it."""
        since = (now - timedelta(days=30)).isoformat(timespec="milliseconds")
        recent = [p for p in store.list_payments() if p["created_at"] >= since and not p["actor"].endswith("(simulated)")]
        blocked = [p for p in recent if p["status"] == "blocked"]
        last = max((p["created_at"] for p in blocked), default=None)
        return {
            "window_days": 30,
            "requests": len(recent),
            "sent": sum(1 for p in recent if p["status"] == "executed"),
            "held_for_owner": sum(1 for p in recent if p["decision"] in ("needs_owner", "owner_approved", "owner_rejected", "expired")),
            "blocked": len(blocked),
            "last_blocked_at": last[:19] if last else None,
            "audit_log_intact": store.verify_chain()["ok"],
        }

    def _passport_claims(mandate: Mandate) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        return {
            "v": 1,
            "type": "countersign.passport",
            "issuer": notary.issuer,
            "key_id": notary.key_id,
            "agent": mandate.agent_name,
            "acts_for": settings.business_name,
            "in_force": mandate.active and not (mandate.expires_on and now.date() > mandate.expires_on),
            "currency": mandate.currency,
            "may": {"payouts": mandate.allow_payouts, "invoices": mandate.allow_invoices, "purchases": mandate.allow_purchases},
            # Amounts as decimal strings, like PayPal's, so the signed bytes survive any JSON parser.
            "limits": {
                "per_payment": _amount(mandate.cap_at(now)),
                "without_owner_signature": _amount(mandate.sign_off_at(now)),
                "weekly": _amount(mandate.weekly_budget),
                "daily": _amount(mandate.daily_budget),
                "first_payment_to_new_payee": _amount(mandate.new_payee_cap),
            },
            "approved_payees": len(mandate.allowed_payees),
            "approved_domains": mandate.allowed_domains,
            # The five things AP2 asks an intent mandate to carry (ap2-protocol.org, core concepts):
            # payer and payee identities, payment methods, the intent and its limits, the agent's
            # natural-language understanding, and a time to live.
            "payer": {"business": settings.business_name, "via": "PayPal"},
            "payment_methods": [name for name, on in (("PayPal Payouts", mandate.allow_payouts),
                                                      ("PayPal Invoicing", mandate.allow_invoices),
                                                      ("PayPal Checkout", mandate.allow_purchases)) if on],
            "categories": sorted(mandate.category_budgets) or None,
            "natural_language": mandate.text[:1200],
            "track_record": _track_record(now),
            "mandate_sha256": mandate_fingerprint(mandate),
            "mandate_expires": mandate.expires_on.isoformat() if mandate.expires_on else None,
            "issued_at": now.isoformat(timespec="seconds"),
            "valid_until": (now + timedelta(hours=24)).isoformat(timespec="seconds"),
        }

    @app.get("/api/passport")
    async def passport() -> dict[str, Any]:
        """A signed statement of what this agent may spend, for a merchant or another agent to check before it transacts."""
        mandate = store.get_mandate()
        if not mandate:
            raise HTTPException(404, "No mandate has been set.")
        claims = _passport_claims(mandate)
        return {"passport": claims, "signature": notary.sign(claims), **notary.public_info()}

    @app.post("/api/passport/verify")
    async def verify_passport(body: PassportBody) -> dict[str, Any]:
        claims = body.passport
        signed = claims.get("type") == "countersign.passport" and notary.verify(claims, body.signature)
        mandate = store.get_mandate()
        current = bool(mandate) and claims.get("mandate_sha256") == mandate_fingerprint(mandate)
        try:
            fresh = datetime.fromisoformat(str(claims.get("valid_until"))) > datetime.now(timezone.utc)
        except ValueError:
            fresh = False
        checks = [
            {"label": "Signed by this Countersign", "ok": signed,
             "detail": "The signature checks out." if signed else "The signature does not match. Something was changed."},
            {"label": "Rules unchanged since it was issued", "ok": current,
             "detail": "The owner has not changed the mandate since." if current else "The owner changed the rules since this passport was issued. Ask for a new one."},
            {"label": "Not expired", "ok": fresh, "detail": "Passports are valid for 24 hours."},
            {"label": "Mandate in force", "ok": bool(claims.get("in_force")) and bool(mandate and mandate.active),
             "detail": "The agent may spend right now." if mandate and mandate.active else "The owner has paused this agent."},
        ]
        return {"valid": all(c["ok"] for c in checks), "checks": checks}

    @app.post("/api/mandate/compile")
    async def compile_and_activate(body: TextBody) -> Mandate:
        try:
            mandate = await compile_mandate(llm, body.text)
        except LLMError as exc:
            raise HTTPException(502, str(exc)) from exc
        saved = store.save_mandate(mandate)
        bus.publish("mandate", saved.model_dump(mode="json"))
        return saved

    @app.post("/api/mandate/preview")
    async def preview_mandate(draft: Mandate) -> dict[str, Any]:
        """Replay past requests against a draft mandate without saving anything.

        Only the stateless rules are replayed (limits, payee lists, payment types). The AI
        verdict each request already received is kept, since a draft can never loosen it.
        """
        was_for = {"auto_approved": "allow", "needs_owner": "escalate", "owner_approved": "escalate",
                   "owner_rejected": "escalate", "blocked_policy": "deny", "blocked_ai": "deny"}
        rows: list[dict[str, Any]] = []
        counts = {"allow": 0, "escalate": 0, "deny": 0}
        for p in store.list_payments()[:50]:
            was = was_for.get(p["decision"])
            if was is None:
                continue
            intent = PaymentIntent(
                rail=p["rail"], counterparty_email=p["counterparty_email"], counterparty_name=p["counterparty_name"],
                amount=p["amount"], currency=p["currency"], purpose=p["purpose"], source_ref=p["source_ref"],
            )
            checks = policy.evaluate(draft, intent, None, p["evidence"])
            ai_status = (p.get("review") or {}).get("status")
            if ai_status:
                checks.append({"status": ai_status})
            now = policy.outcome_of(checks)
            counts[now] += 1
            rows.append({"id": p["id"], "who": p["counterparty_name"] or p["counterparty_email"], "amount": p["amount"],
                         "currency": p["currency"], "was": was, "now": now, "changed": was != now})
        return {"total": len(rows), "counts": counts, "changed": sum(1 for r in rows if r["changed"]), "rows": rows}

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

    drill_state: dict[str, Any] = {"running": False}

    async def run_drill(n: int) -> None:
        drill_state.update(running=True, error="", results=[])
        bus.publish("drill", {**drill_state})
        try:
            attacks = await drill.write_attacks(llm, n)
            items = []
            for attack in attacks:
                item = store.add_inbox_item(
                    {"id": "drill-" + uuid.uuid4().hex[:6], "kind": "email", "sender": attack["sender"],
                     "subject": attack["subject"], "body": attack["body"]}
                )
                store.update_inbox_item(item["id"], status="queued")
                bus.publish("inbox", {"id": item["id"], "status": "queued", "added": True})
                items.append((attack, item["id"]))
            await run_items([item_id for _, item_id in items])
            results = []
            for attack, item_id in items:
                payments = [p for p in store.list_payments() if p["source_ref"] == item_id]
                results.append({"technique": attack["technique"], "subject": attack["subject"], "item_id": item_id,
                                "payment_ids": [p["id"] for p in payments], **drill.outcome_for(payments)})
            stopped = sum(1 for r in results if r["stopped"] == "yes")
            drill_state.update(results=results, stopped=stopped, total=len(results))
            store.audit("attack_drill", "countersign", {"stopped": stopped, "total": len(results),
                                                         "techniques": [r["technique"] for r in results]})
        except LLMError as exc:
            drill_state["error"] = str(exc)
        finally:
            drill_state["running"] = False
            bus.publish("drill", {**drill_state})

    @app.get("/api/drill")
    async def drill_status() -> dict[str, Any]:
        return drill_state

    @app.post("/api/drill", status_code=202)
    async def start_drill(n: int = 4) -> dict[str, Any]:
        """Have the AI write fresh attacks on the agent, run them, and score the defence."""
        if drill_state["running"]:
            raise HTTPException(409, "A drill is already running.")
        drill_state["running"] = True
        spawn(run_drill(max(1, min(n, 6))))
        return {"started": True}

    @app.post("/api/inbox/email", status_code=202)
    async def stranger_email(body: EmailBody) -> dict[str, Any]:
        """Drop an email from an unknown sender into the inbox, so anyone can try to trick the agent."""
        text = body.body.strip()[:4000]
        if not text:
            raise HTTPException(400, "Write the email first.")
        item = store.add_inbox_item(
            {
                "id": "mail-" + uuid.uuid4().hex[:6],
                "kind": "email",
                "sender": (body.sender.strip() or "A stranger <stranger@unknown-sender.example.net>")[:120],
                "subject": (body.subject.strip() or text.splitlines()[0])[:90],
                "body": text,
            }
        )
        store.update_inbox_item(item["id"], status="queued")
        bus.publish("inbox", {"id": item["id"], "status": "queued", "added": True})
        spawn(run_items([item["id"]]))
        return {"queued": [item["id"]]}

    # ---- payments ----------------------------------------------------------

    @app.post("/api/intents")
    async def submit_intent(body: IntentBody, x_countersign_key: Optional[str] = Header(default=None)) -> dict[str, Any]:
        """The door for any agent, in any framework: ask here, and Countersign decides.

        Send the key the owner issued in the X-Countersign-Key header. Without one, the request is
        treated as an unregistered agent, which the owner can choose to refuse outright.
        """
        intent = PaymentIntent.model_validate(body.model_dump(exclude={"actor"}))
        if x_countersign_key:
            registered = registry.by_key(x_countersign_key)
            if not registered:
                raise HTTPException(401, "That agent key is not known.")
            if not registered["active"]:
                raise HTTPException(403, f"The owner revoked \"{registered['name']}\".")
            return await guard.submit(intent, actor=registered["name"], agent=registered)
        mandate = store.get_mandate()
        unregistered = {"unregistered": True} if mandate and mandate.require_registered_agents else None
        return await guard.submit(intent, actor=body.actor[:60] or "external-agent", agent=unregistered)

    # ---- agent identities ------------------------------------------------------

    @app.get("/api/agents")
    async def list_agents() -> list[dict[str, Any]]:
        week_ago = datetime.now(timezone.utc) - timedelta(days=7)
        return [{**a, "used_this_week": store.committed_by_actor(a["name"], week_ago) / 100} for a in registry.list()]

    @app.post("/api/agents")
    async def create_agent(body: AgentBody) -> dict[str, Any]:
        try:
            agent_record, key = registry.create(body.name, body.weekly_allowance)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        bus.publish("agents", {})
        return {"agent": agent_record, "key": key}

    @app.delete("/api/agents/{agent_id}")
    async def revoke_agent(agent_id: str) -> dict[str, Any]:
        try:
            revoked = registry.revoke(agent_id)
        except KeyError as exc:
            raise HTTPException(404, "No such agent.") from exc
        bus.publish("agents", {})
        return revoked

    # ---- standing orders -------------------------------------------------------

    @app.get("/api/standing-orders")
    async def list_standing() -> list[dict[str, Any]]:
        return guard.standing.list()

    @app.post("/api/standing-orders")
    async def create_standing(body: StandingBody) -> dict[str, Any]:
        try:
            order = guard.standing.create(body.name or body.email, body.email, body.amount, body.every_days, body.purpose)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        bus.publish("standing", {})
        return order

    @app.post("/api/standing-orders/{order_id}/run")
    async def run_standing_now(order_id: str) -> dict[str, Any]:
        try:
            payment = await guard.run_standing(order_id)
        except GuardError as exc:
            raise HTTPException(404, str(exc)) from exc
        bus.publish("standing", {})
        return payment

    @app.delete("/api/standing-orders/{order_id}")
    async def stop_standing(order_id: str) -> dict[str, Any]:
        try:
            order = guard.standing.stop(order_id)
        except KeyError as exc:
            raise HTTPException(404, "No such standing order.") from exc
        bus.publish("standing", {})
        return order

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
        return {**record, "audit": list(reversed(store.list_audit(payment_id))), "payee": store.payee_summary(record["counterparty_email"])}

    async def owner_action(action, payment_id: str, *args: Any) -> dict[str, Any]:
        try:
            return await action(payment_id, *args)
        except GuardError as exc:
            raise HTTPException(409, str(exc)) from exc
        except PayPalError as exc:
            raise HTTPException(502, f"PayPal: {exc.message}") from exc

    @app.post("/api/payments/{payment_id}/approve")
    async def approve(payment_id: str, body: NoteBody = NoteBody()) -> dict[str, Any]:
        return await owner_action(guard.approve, payment_id, body.note, body.signer, body.remember_payee, body.after_invoice)

    @app.post("/api/payments/{payment_id}/remind")
    async def remind(payment_id: str) -> dict[str, Any]:
        return await owner_action(guard.remind, payment_id)

    @app.post("/api/preview/invoices/{payment_id}/paid", include_in_schema=False)
    async def preview_client_pays(payment_id: str) -> dict[str, Any]:
        """Offline preview only: act as the client paying the invoice, so pay-when-paid can be tried without PayPal."""
        if not settings.offline_preview:
            raise HTTPException(404, "Only in offline preview. In the sandbox, pay the invoice as the client.")
        payment = store.get_payment(payment_id)
        if not payment or payment["rail"] != "invoice":
            raise HTTPException(404, "No such invoice.")
        paypal.paid_invoices.add((payment.get("paypal") or {}).get("id", ""))
        return await owner_action(guard.sync, payment_id)

    @app.post("/api/mandate/boost")
    async def boost(body: BoostBody) -> Mandate:
        """Raise the cap and sign-off threshold for a while. They drop back by themselves."""
        mandate = store.get_mandate()
        if not mandate:
            raise HTTPException(404, "No mandate has been set.")
        minutes = max(5, min(body.minutes, 24 * 60))
        mandate.boost_limit = max(0.0, body.limit)
        mandate.boost_until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
        saved = store.save_mandate(mandate)
        bus.publish("mandate", saved.model_dump(mode="json"))
        return saved

    @app.delete("/api/mandate/boost")
    async def end_boost() -> Mandate:
        mandate = store.get_mandate()
        if not mandate:
            raise HTTPException(404, "No mandate has been set.")
        mandate.boost_limit, mandate.boost_until = None, None
        saved = store.save_mandate(mandate)
        bus.publish("mandate", saved.model_dump(mode="json"))
        return saved

    @app.post("/api/payments/{payment_id}/reject")
    async def reject(payment_id: str, body: NoteBody = NoteBody()) -> dict[str, Any]:
        return await owner_action(guard.reject, payment_id, body.note)

    @app.post("/api/payments/{payment_id}/undo")
    async def undo(payment_id: str) -> dict[str, Any]:
        return await owner_action(guard.undo, payment_id)

    @app.post("/api/payments/{payment_id}/send-now")
    async def send_now(payment_id: str) -> dict[str, Any]:
        return await owner_action(guard.release, payment_id)

    @app.post("/api/payments/{payment_id}/reverse")
    async def reverse(payment_id: str) -> dict[str, Any]:
        return await owner_action(guard.reverse, payment_id)

    @app.post("/paypal/webhook", include_in_schema=False)
    async def paypal_webhook(request: Request) -> dict[str, Any]:
        """PayPal pushes status changes here. Each event is checked with PayPal before it is believed."""
        try:
            event = json.loads(await request.body())
        except ValueError as exc:
            raise HTTPException(400, "Not a JSON event.") from exc
        if not isinstance(event, dict):
            raise HTTPException(400, "Not a JSON event.")
        if not settings.offline_preview:
            if not settings.paypal_webhook_id:
                raise HTTPException(403, "Set PAYPAL_WEBHOOK_ID to accept PayPal webhooks.")
            try:
                genuine = await paypal.verify_webhook(dict(request.headers), event, settings.paypal_webhook_id)
            except PayPalError as exc:
                raise HTTPException(502, f"PayPal could not check the webhook: {exc.message}") from exc
            if not genuine:
                store.audit("paypal_webhook_rejected", "countersign", {"event_type": str(event.get("event_type", ""))})
                raise HTTPException(400, "This webhook did not pass PayPal's signature check.")
        updated = await guard.apply_webhook(event)
        return {"ok": True, "payment_id": updated["id"] if updated else None}

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

    # ---- signed proofs -----------------------------------------------------

    @app.get("/verify/{payment_id}", include_in_schema=False)
    async def verify_page(payment_id: str) -> FileResponse:
        return FileResponse(WEB_DIR / "verify.html", headers={"Cache-Control": "no-store"})

    @app.get("/.well-known/countersign-key.json")
    async def public_key() -> dict[str, str]:
        """The public half of the signing key. Anyone can use it to check a proof offline."""
        return notary.public_info()

    @app.get("/api/proofs/{payment_id}")
    async def get_proof(payment_id: str) -> dict[str, Any]:
        record = store.get_proof(payment_id)
        payment = store.get_payment(payment_id)
        if not record:
            status = payment["status"] if payment else None
            raise HTTPException(404, "No proof yet: this payment has not reached a final decision." if status else "No such payment.")
        return {"proof": record["data"], "signature": record["signature"], **notary.public_info()}

    @app.post("/api/proofs/verify")
    async def verify_proof(body: ProofBody) -> dict[str, Any]:
        signed = notary.verify(body.proof, body.signature)
        stored = store.get_proof(str(body.proof.get("payment_id", "")))
        in_ledger = bool(stored) and canonical(stored["data"]) == canonical(body.proof)
        chain = store.verify_chain()
        checks = [
            {
                "label": "Signed by this Countersign",
                "ok": signed,
                "detail": f"Ed25519 signature checks out against key {notary.key_id}."
                if signed else "The signature does not match these contents. Something in the proof was changed.",
            },
            {
                "label": "Matches the ledger",
                "ok": in_ledger,
                "detail": "Identical to the proof recorded when the decision was made."
                if in_ledger else "This is not the proof that was recorded for this payment.",
            },
            {
                "label": "Audit log unbroken",
                "ok": chain["ok"],
                "detail": f"All {chain['count']} entries chain correctly." if chain["ok"] else f"The audit log was altered at entry {chain.get('broken_at')}.",
            },
        ]
        return {"valid": all(c["ok"] for c in checks), "checks": checks}

    # ---- approve from your phone ------------------------------------------------

    APPROVAL_MINUTES = 10

    def _approval(token: str) -> tuple[dict[str, Any], dict[str, Any]]:
        raw = store.get_kv(f"approval:{token}")
        if not raw:
            raise HTTPException(404, "This approval link does not exist.")
        grant = json.loads(raw)
        if grant.get("used"):
            raise HTTPException(410, "This link has already been used. Each link works once.")
        if datetime.fromisoformat(grant["expires_at"]) < datetime.now(timezone.utc):
            raise HTTPException(410, "This link has expired. Ask for a new one on the console.")
        payment = store.get_payment(grant["payment_id"])
        if not payment or payment["status"] != "pending_approval":
            raise HTTPException(410, "This payment is no longer waiting for a signature.")
        return grant, payment

    @app.post("/api/payments/{payment_id}/approval-link")
    async def approval_link(payment_id: str) -> dict[str, Any]:
        """A single-use link, valid for ten minutes, to sign one held payment from a phone."""
        payment = store.get_payment(payment_id)
        if not payment or payment["status"] != "pending_approval":
            raise HTTPException(409, "Only a payment that is waiting for you can be signed from a phone.")
        token = secrets.token_urlsafe(18)
        expires = (datetime.now(timezone.utc) + timedelta(minutes=APPROVAL_MINUTES)).isoformat(timespec="seconds")
        store.set_kv(f"approval:{token}", json.dumps({"payment_id": payment_id, "expires_at": expires, "used": False}))
        store.audit("approval_link_created", "owner", {"expires_at": expires}, payment_id)
        url = f"{settings.public_base_url}/approve/{token}"
        svg = segno.make(url, error="m").svg_inline(scale=5, dark="#001435", light="#ffffff", border=2)
        return {"url": url, "expires_at": expires, "qr_svg": svg}

    # ---- payee confirms their own address ----------------------------------------

    CONFIRM_DAYS = 7

    @app.post("/api/payments/{payment_id}/confirm-link")
    async def confirm_link(payment_id: str) -> dict[str, Any]:
        """A link for a new payee to confirm that the PayPal address really is theirs, before the owner can sign."""
        payment = store.get_payment(payment_id)
        if not payment or payment["status"] != "pending_approval":
            raise HTTPException(409, "Only a payment that is waiting for you needs a confirmation.")
        token = secrets.token_urlsafe(18)
        expires = (datetime.now(timezone.utc) + timedelta(days=CONFIRM_DAYS)).isoformat(timespec="seconds")
        store.set_kv(f"confirm:{token}", json.dumps({"payment_id": payment_id, "email": payment["counterparty_email"], "expires_at": expires, "used": False}))
        store.audit("confirmation_link_created", "owner", {"expires_at": expires}, payment_id)
        url = f"{settings.public_base_url}/confirm/{token}"
        message = (f"Hi {payment['counterparty_name'] or ''}, before {settings.business_name} pays you "
                   f"{payment['amount']:,.2f} {payment['currency']} for {payment['purpose']}, please confirm your PayPal address: {url}").replace("Hi ,", "Hi,")
        return {"url": url, "expires_at": expires, "message": message}

    def _confirmation(token: str) -> tuple[dict[str, Any], dict[str, Any]]:
        raw = store.get_kv(f"confirm:{token}")
        if not raw:
            raise HTTPException(404, "This confirmation link does not exist.")
        grant = json.loads(raw)
        if grant.get("used"):
            raise HTTPException(410, "This link has already been used.")
        if datetime.fromisoformat(grant["expires_at"]) < datetime.now(timezone.utc):
            raise HTTPException(410, "This link has expired. Ask the business for a new one.")
        payment = store.get_payment(grant["payment_id"])
        if not payment:
            raise HTTPException(410, "This payment no longer exists.")
        return grant, payment

    @app.get("/confirm/{token}", include_in_schema=False)
    async def confirm_page(token: str) -> FileResponse:
        return FileResponse(WEB_DIR / "confirm.html", headers={"Cache-Control": "no-store"})

    @app.get("/api/confirmations/{token}")
    async def confirmation_details(token: str) -> dict[str, Any]:
        grant, payment = _confirmation(token)
        return {"business": settings.business_name, "email": grant["email"], "amount": payment["amount"],
                "currency": payment["currency"], "purpose": payment["purpose"], "name": payment["counterparty_name"]}

    @app.post("/api/confirmations/{token}")
    async def confirmation_action(token: str, body: ActionBody) -> dict[str, Any]:
        grant, payment = _confirmation(token)
        if body.action not in ("confirm", "deny"):
            raise HTTPException(400, "Choose confirm or deny.")
        grant["used"] = True
        store.set_kv(f"confirm:{token}", json.dumps(grant))
        if body.action == "confirm":
            store.confirm_payee(grant["email"])
            store.audit("payee_confirmed", grant["email"], {}, payment["id"])
            text = f"{grant['email']} confirmed by the payee."
        else:
            store.audit("payee_denied", grant["email"], {}, payment["id"])
            if payment["status"] == "pending_approval":
                await guard.reject(payment["id"], "The payee said this PayPal address is not theirs.")
            text = f"{grant['email']}: the payee said this address is not theirs. The payment was refused."
        bus.publish("alert", {"kind": "confirmation", "text": text})
        fresh = store.get_payment(payment["id"])
        if fresh:
            bus.publish("payment", fresh)
        return {"ok": True, "action": body.action}

    # ---- payees: trust and statements ---------------------------------------------

    @app.get("/api/payees")
    async def payees() -> list[dict[str, Any]]:
        mandate = store.get_mandate() or Mandate()
        confirmations = store.payee_confirmations()
        year = str(datetime.now(timezone.utc).year)
        people: dict[str, dict[str, Any]] = {}
        for p in reversed(store.list_payments()):
            row = people.setdefault(p["counterparty_email"], {
                "email": p["counterparty_email"], "name": "", "paid_count": 0, "billed_count": 0, "paid_ytd": 0, "billed_ytd": 0,
                "stopped": 0, "first_seen": p["created_at"], "last_paid": None})
            row["name"] = p["counterparty_name"] or row["name"]
            if p["status"] == "executed" and p["created_at"].startswith(year):
                if p["rail"] == "invoice":
                    row["billed_ytd"] += p["amount_cents"]
                    row["billed_count"] += 1
                else:
                    row["paid_ytd"] += p["amount_cents"]
                    row["paid_count"] += 1
                    row["last_paid"] = p["created_at"]
            if p["status"] in ("blocked", "rejected"):
                row["stopped"] += 1
        out = []
        for row in people.values():
            approved = mandate.payee_approved(row["email"])
            if row["stopped"] and not row["paid_count"]:
                trust, why = "risky", f"Stopped {row['stopped']} time{'s' if row['stopped'] > 1 else ''}, never paid"
            elif row["paid_count"] >= 2 and not row["stopped"]:
                trust, why = "trusted", f"Paid {row['paid_count']} times, never stopped"
            elif row["billed_count"] and not row["paid_count"]:
                trust, why = "known", f"Client, billed {row['billed_count']} time{'s' if row['billed_count'] > 1 else ''}"
            elif row["paid_count"]:
                trust, why = "known", f"Paid {row['paid_count']} time{'s' if row['paid_count'] > 1 else ''}" + (f", stopped {row['stopped']}" if row["stopped"] else "")
            else:
                trust, why = "new", "Never paid" + (", on your approved list" if approved else "")
            out.append({**row, "paid_ytd": row["paid_ytd"] / 100, "billed_ytd": row["billed_ytd"] / 100, "approved": approved,
                        "confirmed": row["email"] in confirmations, "trust": trust, "trust_text": why})
        return sorted(out, key=lambda r: (-r["paid_ytd"] - r["billed_ytd"], r["email"]))

    @app.get("/api/payees/{email}/statement")
    async def statement(email: str) -> StreamingResponse:
        """A year-to-date statement for one payee, to send them for their own records or taxes."""
        email = email.strip().lower()
        year = str(datetime.now(timezone.utc).year)
        rows = [p for p in reversed(store.list_payments())
                if p["counterparty_email"] == email and p["status"] == "executed" and p["created_at"].startswith(year)]
        if not rows:
            raise HTTPException(404, "Nothing was paid to or billed to this address this year.")
        lines = [f"Statement from {settings.business_name} for {email}, {year} to date",
                 "date,type,amount,currency,purpose,paypal_reference,countersign_id,proof"]
        def cell(text: str) -> str:
            text = str(text or "")
            if text[:1] in ("=", "+", "-", "@"):
                text = "'" + text
            return '"' + text.replace('"', '""') + '"'
        total = 0
        for p in rows:
            total += p["amount_cents"] if p["rail"] != "invoice" else 0
            lines.append(",".join([p["created_at"][:10], {"payout": "Paid to you", "invoice": "Invoiced to you", "purchase": "Purchase"}[p["rail"]],
                                   f"{p['amount_cents'] / 100:.2f}", p["currency"], cell(p["purpose"]),
                                   cell((p.get("paypal") or {}).get("id", "")), p["id"], guard.verify_url(p["id"])]))
        lines.append(f",Total paid to you,{total / 100:.2f},{rows[0]['currency']},,,,")
        body = "\n".join(lines) + "\n"
        name = email.replace("@", "_at_")
        return StreamingResponse(iter([body]), media_type="text/csv",
                                 headers={"Content-Disposition": f'attachment; filename="statement-{name}-{year}.csv"'})

    @app.get("/approve/{token}", include_in_schema=False)
    async def approve_page(token: str) -> FileResponse:
        return FileResponse(WEB_DIR / "approve.html", headers={"Cache-Control": "no-store"})

    @app.get("/api/approvals/{token}")
    async def approval_details(token: str) -> dict[str, Any]:
        grant, payment = _approval(token)
        return {
            "expires_at": grant["expires_at"],
            "payment": {key: payment[key] for key in ("id", "rail", "counterparty_name", "counterparty_email", "amount", "currency", "purpose", "actor")},
            "reasons": [c["detail"] for c in payment["checks"] if c["status"] != "pass"],
            "business": settings.business_name,
        }

    @app.post("/api/approvals/{token}")
    async def approval_action(token: str, body: ActionBody) -> dict[str, Any]:
        grant, payment = _approval(token)
        if body.action not in ("approve", "reject"):
            raise HTTPException(400, "Choose approve or reject.")
        grant["used"] = True
        store.set_kv(f"approval:{token}", json.dumps(grant))
        result = await (owner_action(guard.approve, payment["id"], "Signed from a phone.", "Owner (phone)") if body.action == "approve"
                        else owner_action(guard.reject, payment["id"], "Refused from a phone."))
        return {"status": result["status"], "payment_id": result["id"]}

    # ---- evidence pack for disputes --------------------------------------------

    @app.get("/api/payments/{payment_id}/evidence")
    async def evidence(payment_id: str) -> JSONResponse:
        """Everything about one payment in one file: what a dispute team or an auditor would ask for."""
        record = store.get_payment(payment_id)
        if not record:
            raise HTTPException(404, "No such payment.")
        proof = store.get_proof(payment_id)
        pack = {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "issuer": settings.business_name,
            "payment": {k: v for k, v in record.items() if k not in ("evidence", "checks", "review", "paypal")},
            "source_record": record["evidence"],
            "policy_and_ai_checks": record["checks"],
            "ai_review": record["review"],
            "paypal": record["paypal"],
            "mandate_sha256": record.get("mandate_hash", ""),
            "signed_proof": {"proof": proof["data"], "signature": proof["signature"]} if proof else None,
            "verify_url": guard.verify_url(payment_id),
            "public_key": notary.public_info(),
            "payee_history": store.payee_summary(record["counterparty_email"]),
            "audit_trail": list(reversed(store.list_audit(payment_id))),
            "audit_chain": store.verify_chain(),
        }
        return JSONResponse(pack, headers={"Content-Disposition": f'attachment; filename="countersign-evidence-{payment_id}.json"'})

    # ---- ask your money ------------------------------------------------------

    @app.post("/api/ask")
    async def ask_ledger(body: TextBody) -> dict[str, Any]:
        """Answer a question about the money, from the ledger only, citing the payments used."""
        try:
            return await ask.answer(llm, body.text, store.get_mandate(), store.list_payments())
        except LLMError as exc:
            raise HTTPException(502, str(exc)) from exc

    @app.post("/api/briefing")
    async def briefing() -> dict[str, Any]:
        """What the agent did in the last 24 hours: exact figures from the ledger, then a short written summary."""
        since = datetime.now(timezone.utc) - timedelta(hours=24)
        recent = [p for p in store.list_payments() if datetime.fromisoformat(p["created_at"]) >= since]
        out = [p for p in recent if p["rail"] in MONEY_OUT_RAILS]
        figures = {
            "requests": len(recent),
            "paid_out": sum(p["amount_cents"] for p in out if p["status"] == "executed") / 100,
            "billed": sum(p["amount_cents"] for p in recent if p["rail"] == "invoice" and p["status"] in ("executed", "awaiting_payer")) / 100,
            "stopped": sum(p["amount_cents"] for p in out if p["status"] in ("blocked", "rejected")) / 100,
            "waiting": sum(1 for p in recent if p["status"] == "pending_approval"),
            "currency": (store.get_mandate() or Mandate()).currency,
        }
        summary = None
        if recent:
            try:
                summary = await ask.answer(
                    llm,
                    "Write the owner's end-of-day briefing for the last 24 hours in at most four short sentences: "
                    "what was paid, what was stopped and why, and what is waiting for a signature. Cite the payments.",
                    store.get_mandate(), recent,
                )
            except LLMError as exc:
                summary = {"answer": f"The written summary is unavailable: {exc}", "cites": []}
        return {"figures": figures, "summary": summary}

    # ---- payment run planner ---------------------------------------------------------

    def _goal() -> planner.PlannerGoal:
        raw = store.get_kv("planner_goal")
        return planner.PlannerGoal.model_validate_json(raw) if raw else planner.PlannerGoal()

    def _plans() -> list[dict[str, Any]]:
        return json.loads(store.get_kv("plans") or "[]")

    def _save_plans(plans: list[dict[str, Any]]) -> None:
        store.set_kv("plans", json.dumps(plans[-20:]))

    @app.get("/api/planner/goal")
    async def get_goal() -> planner.PlannerGoal:
        return _goal()

    @app.post("/api/planner/goal")
    async def set_goal_from_words(body: TextBody) -> planner.PlannerGoal:
        """The model turns the owner's words into a goal for the optimiser. It is validated before it is used."""
        try:
            goal = await planner.compile_goal(llm, body.text)
        except LLMError as exc:
            raise HTTPException(502, str(exc)) from exc
        store.set_kv("planner_goal", goal.model_dump_json())
        store.audit("planner_goal_set", "owner", goal.model_dump())
        return goal

    @app.put("/api/planner/goal")
    async def put_goal(goal: planner.PlannerGoal) -> planner.PlannerGoal:
        store.set_kv("planner_goal", goal.model_dump_json())
        return goal

    @app.post("/api/planner/plan")
    async def make_plan(body: PlanBody = PlanBody()) -> dict[str, Any]:
        """Choose which held payments to send today. The optimiser decides; the model only explains."""
        goal = _goal()
        overrides = {k: v for k, v in body.model_dump().items() if v}
        if overrides:
            goal = planner.PlannerGoal.model_validate({**goal.model_dump(), **overrides})
        payments = store.list_payments()
        balance = await guard.balance() if paypal.configured else None
        trust: dict[str, int] = {}
        for p in payments:
            if p["status"] == "executed" and p["rail"] in MONEY_OUT_RAILS:
                trust[p["counterparty_email"]] = trust.get(p["counterparty_email"], 0) + 1
        result = planner.build_plan(payments=payments, store=store, goal=goal, balance=balance / 100 if balance is not None else None,
                                    confirmed=store.payee_confirmations(), trust=trust)
        record = {"id": "plan_" + uuid.uuid4().hex[:10], "status": "proposed", "result": result,
                  "explanation": await planner.explain(llm, result)}
        plans = _plans()
        plans.append(record)
        _save_plans(plans)
        store.audit("plan_made", "planner", {"plan": record["id"], "inputs_sha256": result["inputs_sha256"], "pay": result["plan"]["ids"]})
        return record

    @app.get("/api/planner/plans")
    async def list_plans() -> list[dict[str, Any]]:
        """Every plan with its check: what it expected against what then happened."""
        by_id = {p["id"]: p for p in store.list_payments()}
        balance = await guard.balance() if paypal.configured else None
        out = []
        for record in reversed(_plans()):
            item = {k: record[k] for k in ("id", "status")} | {
                "created_at": record["result"]["created_at"], "inputs_sha256": record["result"]["inputs_sha256"],
                "applied_at": record.get("applied_at"), "pay": record["result"]["plan"]["ids"]}
            if record["status"] == "applied":
                item["check"] = planner.check_plan(record, by_id, balance / 100 if balance is not None else None)
            out.append(item)
        return out

    @app.post("/api/planner/plans/{plan_id}/apply")
    async def apply_plan(plan_id: str, body: ApplyBody = ApplyBody()) -> dict[str, Any]:
        """Sign every payment the plan pays today, in one go. The rest stay waiting."""
        plans = _plans()
        record = next((r for r in plans if r["id"] == plan_id), None)
        if not record:
            raise HTTPException(404, "No such plan.")
        if record["status"] == "applied":
            raise HTTPException(409, "This plan has already been signed.")
        results = []
        for pid in record["result"]["plan"]["ids"]:
            try:
                p = await guard.approve(pid, f"Signed as part of payment plan {plan_id}.", body.signer)
                results.append({"id": pid, "status": p["status"]})
            except (GuardError, PayPalError) as exc:
                results.append({"id": pid, "status": "skipped", "reason": str(exc)})
        record.update(status="applied", applied_at=datetime.now(timezone.utc).isoformat(timespec="seconds"), results=results)
        _save_plans(plans)
        store.audit("plan_signed", body.signer, {"plan": plan_id, "results": results})
        return record

    # ---- mandate oversight check -------------------------------------------------------

    def _docs() -> list[dict[str, Any]]:
        return json.loads(store.get_kv("policy_docs") or "[]")

    def _knowledge() -> dict[str, str]:
        return json.loads(store.get_kv("owner_knowledge") or "{}")

    async def _add_doc(name: str, text: str) -> dict[str, Any]:
        if not text.strip():
            raise HTTPException(400, "The document is empty.")
        doc = await oversight.ingest(llm, name.strip() or "Policy", text[:60000])
        doc["id"] = "doc_" + uuid.uuid4().hex[:8]
        docs = [d for d in _docs() if d["name"] != doc["name"]] + [doc]
        store.set_kv("policy_docs", json.dumps(docs[-10:]))
        store.audit("policy_document_added", "owner", {"name": doc["name"], "sections": len(doc["chunks"])})
        return {k: doc[k] for k in ("id", "name", "chunks", "summary")}

    @app.get("/api/policy-docs")
    async def list_docs() -> list[dict[str, Any]]:
        return [{k: d[k] for k in ("id", "name", "chunks", "summary")} for d in _docs()]

    @app.post("/api/policy-docs")
    async def add_doc(body: DocBody) -> dict[str, Any]:
        return await _add_doc(body.name, body.text)

    @app.post("/api/policy-docs/sample")
    async def add_sample_doc() -> dict[str, Any]:
        return await _add_doc("Spending policy", oversight.SAMPLE_POLICY)

    @app.delete("/api/policy-docs/{doc_id}")
    async def remove_doc(doc_id: str) -> dict[str, Any]:
        store.set_kv("policy_docs", json.dumps([d for d in _docs() if d["id"] != doc_id]))
        return {"ok": True}

    @app.get("/api/oversight")
    async def oversight_check() -> dict[str, Any]:
        mandate = store.get_mandate() or Mandate()
        return oversight.check(mandate, _docs(), store.list_payments(), _knowledge())

    @app.get("/api/oversight/graph")
    async def oversight_graph() -> dict[str, Any]:
        graph = oversight.build_graph(store.get_mandate() or Mandate(), _docs(), store.list_payments())
        return {"nodes": list(graph["nodes"].values()), "edges": [{"from": a, "to": b, "label": r} for a, b, r in graph["edges"]]}

    def _finding(fid: str) -> dict[str, Any]:
        result = oversight.check(store.get_mandate() or Mandate(), _docs(), store.list_payments(), _knowledge())
        found = next((f for f in result["findings"] if f["id"] == fid), None)
        if not found:
            raise HTTPException(404, "That finding no longer applies.")
        return found

    @app.post("/api/oversight/fix")
    async def oversight_fix(body: TextBody) -> Mandate:
        found = _finding(body.text)
        if not found["fix"]:
            raise HTTPException(409, "This one needs your judgement; there is no automatic fix.")
        try:
            mandate = oversight.apply_fix(store.get_mandate() or Mandate(), found["fix"])
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        saved = store.save_mandate(mandate, actor="owner")
        store.audit("oversight_fixed", "owner", {"finding": found["id"], "fix": found["fix"]})
        bus.publish("mandate", saved.model_dump(mode="json"))
        return saved

    @app.post("/api/oversight/intended")
    async def oversight_intended(body: DocBody) -> dict[str, Any]:
        """The owner says a finding is deliberate and why. Kept, and used in every later check."""
        _finding(body.name)
        knowledge = _knowledge()
        knowledge[body.name] = body.text.strip()[:300] or "Intended."
        store.set_kv("owner_knowledge", json.dumps(knowledge))
        store.audit("owner_knowledge_added", "owner", {"finding": body.name, "note": knowledge[body.name]})
        return {"ok": True}

    @app.delete("/api/oversight/intended/{fid:path}")
    async def oversight_unintend(fid: str) -> dict[str, Any]:
        knowledge = _knowledge()
        knowledge.pop(fid, None)
        store.set_kv("owner_knowledge", json.dumps(knowledge))
        return {"ok": True}

    @app.post("/api/rules/ask")
    async def ask_rules(body: TextBody) -> dict[str, Any]:
        try:
            return await oversight.answer(llm, body.text, store.get_mandate() or Mandate(), _docs())
        except LLMError as exc:
            raise HTTPException(502, str(exc)) from exc

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
        drill_state.clear()
        drill_state["running"] = False
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

