"""The payment run planner and the mandate oversight check."""

import asyncio
import itertools
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from countersign import oversight, planner
from countersign.app import create_app
from countersign.config import load_settings
from countersign.inbox import default_mandate
from countersign.models import Mandate
from countersign.preview import PreviewLLM, PreviewPayPal
from countersign.store import Store


def item(i, amount, value, risk, email=None, blocked=""):
    return {"id": f"cs_{i}", "who": f"P{i}", "email": email or f"p{i}@example.com", "amount": amount, "value": value,
            "risk": risk, "blocked": blocked}


def brute_force(items, cash, max_payments, lam):
    best = (0.0, ())
    pool = [i for i in items if not i["blocked"]]
    for r in range(len(pool) + 1):
        for combo in itertools.combinations(pool, r):
            if sum(i["amount"] for i in combo) <= cash and len(combo) <= max_payments:
                obj, _, _ = planner._score(list(combo), lam, cash)
                if obj > best[0] + 1e-9:
                    best = (obj, combo)
    return round(best[0], 2), sorted(i["id"] for i in best[1])


# ---- planner ---------------------------------------------------------------------------------

@pytest.mark.parametrize("lam", [0.0, 0.5, 1.0, 3.0])
def test_optimiser_matches_brute_force(lam):
    items = [item(1, 450, 2250, 40), item(2, 280, 560, 20), item(3, 300, 900, 120, email="p1@example.com"),
             item(4, 49, 147, 4), item(5, 600, 1200, 30), item(6, 120, 600, 90, blocked="not confirmed")]
    for cash, limit in ((900, 10), (1500, 2), (400, 10)):
        got = planner.solve(items, cash, limit, lam)
        assert (got["objective"], got["ids"]) == brute_force(items, cash, limit, lam)


def test_optimiser_respects_cash_count_and_blocked():
    items = [item(1, 500, 5000, 0), item(2, 500, 4000, 0), item(3, 100, 900, 0, blocked="x")]
    got = planner.solve(items, 600, 10, 1.0)
    assert got["ids"] == ["cs_1"] and got["amount"] <= 600
    assert planner.solve(items, 2000, 1, 1.0)["ids"] == ["cs_1"]


def test_concentration_penalty_spreads_cash():
    same = [item(1, 400, 1000, 0, email="a@x.com"), item(2, 400, 1000, 0, email="a@x.com"), item(3, 400, 900, 0, email="b@x.com")]
    assert planner.solve(same, 800, 10, 2.0)["ids"] == ["cs_1", "cs_3"]


def test_goal_is_validated_and_bounded():
    goal = planner.PlannerGoal.model_validate({"priorities": {"contractors": 9, "Nonsense": 3}, "buffer": 100})
    assert goal.priorities["Contractors"] == 5 and "Nonsense" not in goal.priorities
    with pytest.raises(Exception):
        planner.PlannerGoal.model_validate({"buffer": -5})


def test_goal_from_words():
    goal = asyncio.run(planner.compile_goal(PreviewLLM(), "Keep at least $1,000 in PayPal. Contractors first, marketing can wait. Be careful."))
    assert goal.buffer == 1000 and goal.risk_appetite == "low"
    assert goal.priorities["Contractors"] == 5 and goal.priorities["Marketing"] == 1


@pytest.fixture
def api(tmp_path):
    app = create_app(load_settings(), Store(str(tmp_path / "plan.db")), PreviewPayPal(), PreviewLLM())
    with TestClient(app) as client:
        client.put("/api/mandate", json={**client.get("/api/mandate").json(), "undo_seconds": 0})
        yield client


def hold(api, email, name, amount):
    return api.post("/api/intents", json={"rail": "payout", "counterparty_email": email, "counterparty_name": name, "amount": amount,
                                          "purpose": "Extra edits", "justification": "Owner asked", "actor": "Ops Agent"}).json()


def test_plan_explain_apply_and_check(api):
    a = hold(api, "marco.silva@personal.example.com", "Marco Silva", 300)
    b = hold(api, "priya.nair@personal.example.com", "Priya Nair", 280)
    assert a["status"] == b["status"] == "pending_approval"
    api.put("/api/planner/goal", json={"buffer": 2600, "risk_appetite": "medium"})
    record = api.post("/api/planner/plan").json()
    result = record["result"]
    assert result["cash_available"] == 400  # 3,000 balance minus the 2,600 buffer
    assert len(result["plan"]["ids"]) == 1 and result["plan"]["amount"] <= 400
    assert any("binding" in s for s in result["sensitivity"])
    assert {s["scenario"] for s in result["scenarios"]} == {"expected", "cautious", "hopeful"}
    assert result["frontier"] and any(p["chosen"] for p in result["frontier"])
    assert record["explanation"]["summary"]
    assert set(record["explanation"]["cites"]) <= {r["id"] for r in result["rows"]}
    again = api.post("/api/planner/plan").json()["result"]
    assert again["inputs_sha256"] == result["inputs_sha256"] and again["plan"] == result["plan"]
    signed = api.post(f"/api/planner/plans/{record['id']}/apply", json={"signer": "Guru"}).json()
    assert [r["status"] for r in signed["results"]] == ["executed"]
    assert api.post(f"/api/planner/plans/{record['id']}/apply", json={}).status_code == 409
    check = next(p for p in api.get("/api/planner/plans").json() if p["id"] == record["id"])["check"]
    assert check["planned"] == check["sent"] == 1


def test_unconfirmed_new_payee_is_never_planned(api):
    held = hold(api, "ana.ruiz@example.com", "Ana Ruiz", 60)
    result = api.post("/api/planner/plan").json()["result"]
    row = next(r for r in result["rows"] if r["id"] == held["id"])
    assert not row["pay_today"] and "confirmed" in row["why"]


def test_forecast_learns_from_paid_invoices(tmp_path):
    store = Store(str(tmp_path / "f.db"))
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    inv = {"id": "cs_inv", "created_at": now, "updated_at": now, "actor": "a", "rail": "invoice", "counterparty_email": "c@x.com",
           "counterparty_name": "C", "amount_cents": 100000, "currency": "USD", "purpose": "p", "justification": "", "source_ref": None,
           "evidence": "", "items": [], "status": "executed", "decision": "auto_approved", "checks": [], "review": {},
           "paypal": {"id": "INV2-1", "status": "PAID"}, "decided_by": "", "note": "", "mandate_hash": "", "category": "Client billing"}
    store.insert_payment(inv)
    store.audit("paypal_status", "paypal", {"status": "PAID"}, "cs_inv")
    history = planner.client_history(store, store.list_payments())
    assert history["c@x.com"]["paid"] == 1 and history["c@x.com"]["avg_days"] is not None


# ---- oversight -------------------------------------------------------------------------------

def test_chunking_follows_headings_and_falls_back_by_size():
    doc = "# Policy\n\nIntro line.\n\n## A\n\n" + "word " * 30 + "\n\n## B\n\n" + "term " * 200
    chunks = oversight.chunk(doc, cmax=60, overlap=0.1)
    paths = [c["path"] for c in chunks]
    assert ["Policy", "A"] in paths
    b_parts = [c for c in chunks if c["path"] == ["Policy", "B"]]
    assert len(b_parts) >= 4 and all(len(c["text"].split()) <= 60 for c in b_parts)
    assert all(len(c["text"].split()) <= 60 for c in chunks)


def test_summary_keywords_must_come_from_the_section():
    class FakeLLM:
        async def json(self, system, user):
            return {"overview": "Covers travel.", "sections": [{"title": "Travel", "summary": "Travel rules.", "keywords": ["two approvals", "invented term"]}]}
    doc = asyncio.run(oversight.ingest(FakeLLM(), "P", "# P\n\n## Travel\n\nAny travel over $300 needs two approvals."))
    section = next(s for s in doc["summary"]["sections"] if s["title"] == "Travel")
    assert section["keywords"] == ["two approvals"]


def sample(mandate=None):
    m = mandate or default_mandate(load_settings())
    doc = asyncio.run(oversight.ingest(None, "Spending policy", oversight.SAMPLE_POLICY))
    doc["id"] = "d1"
    return m, doc


def test_oversight_finds_conflicts_unenforced_and_silent_gaps():
    m, doc = sample()
    found = oversight.check(m, [doc], [], {})
    titles = [f["title"] for f in found["findings"]]
    kinds = {f["kind"] for f in found["findings"]}
    assert {"Conflict", "Written, not enforced", "Silent default"} <= kinds
    assert any("policy says $400.00" in t for t in titles)
    assert any(t.startswith("Marketing") for t in titles) and any(t.startswith("Travel") for t in titles)
    assert any("vendors" in t for t in titles) and any("Working hours" in t for t in titles)
    assert all(f["evidence"] for f in found["findings"])
    assert found["stats"]["sections"] == 5 and found["stats"]["edges"] > 20


def test_planted_gaps_are_all_found():
    """Self-test: a policy with known gaps; every one must be reported."""
    m = default_mandate(load_settings())
    planted = "# Policy\n\n## Marketing\n\nMarketing is limited to $200 per week.\n\n## Travel\n\nAny travel over $250 needs two approvals.\n\n## Timing\n\nNo payments at weekends."
    doc = asyncio.run(oversight.ingest(None, "Planted", planted)); doc["id"] = "p"
    titles = " | ".join(f["title"] for f in oversight.check(m, [doc], [], {})["findings"])
    assert "Marketing" in titles and "Travel" in titles and "Working hours" in titles


def test_reality_gaps_come_from_the_ledger():
    m = default_mandate(load_settings())
    pay = {"id": "cs_1", "rail": "payout", "status": "executed", "category": "Travel", "amount": 120.0, "counterparty_email": "t@x.com", "counterparty_name": "T"}
    found = oversight.check(m, [], [pay], {})
    assert any(f["id"] == "reality:category:Travel" for f in found["findings"])


def test_fixes_only_tighten():
    m = default_mandate(load_settings())
    tighter = oversight.apply_fix(m, {"field": "per_payment_cap", "value": 400})
    assert tighter.per_payment_cap == 400
    assert oversight.apply_fix(tighter, {"field": "per_payment_cap", "value": 900}).per_payment_cap == 400
    budgets = oversight.apply_fix(m, {"field": "category_budgets", "key": "Marketing", "value": 250})
    assert oversight.apply_fix(budgets, {"field": "category_budgets", "key": "Marketing", "value": 900}).category_budgets["Marketing"] == 250


def test_global_then_local_retrieval():
    _, doc = sample()
    hits = oversight.retrieve("What are the rules for travel?", [doc])
    assert hits and "Travel" in hits[0]["text"]


def test_oversight_api_fix_intend_and_ask(api):
    api.post("/api/policy-docs/sample")
    findings = api.get("/api/oversight").json()["findings"]
    conflict = next(f for f in findings if f["kind"] == "Conflict")
    assert api.post("/api/oversight/fix", json={"text": conflict["id"]}).json()["per_payment_cap"] == 400
    assert not any(f["id"] == conflict["id"] for f in api.get("/api/oversight").json()["findings"])
    other = next(f for f in api.get("/api/oversight").json()["findings"] if not f["fix"])
    api.post("/api/oversight/intended", json={"name": other["id"], "text": "Refunds go through the shop"})
    marked = next(f for f in api.get("/api/oversight").json()["findings"] if f["id"] == other["id"])
    assert marked["intended"] == "Refunds go through the shop"
    assert api.post("/api/oversight/fix", json={"text": other["id"]}).status_code == 409
    graph = api.get("/api/oversight/graph").json()
    assert any(n["kind"] == "summary" for n in graph["nodes"]) and graph["edges"]
    answer = api.post("/api/rules/ask", json={"text": "What are the rules for travel?"}).json()
    assert "travel" in answer["answer"].lower() and answer["sections"]
