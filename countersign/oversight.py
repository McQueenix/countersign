"""Mandate oversight check: find what the owner's rules forgot, using a lightweight knowledge graph.

An owner's spending rules live in more than one place: the mandate they wrote, any policy documents
they keep (an expense policy, a supplier policy), and what the agent actually does with money. A gap
between them is an oversight, and oversights are where money leaks.

How it works:

1. Structure-preserving chunking. Each policy document is split along its own headings, not at a
   fixed size: a section that fits stays whole; a long one is split at its next heading level; only a
   long section with no headings left is cut by size, with a small overlap so no sentence is lost.
   Every chunk keeps its heading path, so "Approvals" stays attached to "Travel".
2. One summary chunk per document: an overview of its scope and of how its sections depend on each
   other, plus an index of every section with a one-line summary and keywords taken from that section.
   The model writes it; every keyword is checked against the section text and invented ones are
   dropped. The summary is the anchor that joins sections which would otherwise sit alone.
3. A knowledge graph. Entities (payees, domains, amounts, categories, payment types, time and
   approval conditions) are extracted from every chunk, from the summary, from the enforced rules and
   from the ledger. The same entity found in two places is one node, so a section, a rule and a
   payment that mention it are connected.
4. Global, then local. Oversight detection first walks the whole graph (which areas exist at all),
   then reads the detailed chunks of each area. Questions about the rules work the same way: the
   summary chunk finds the right sections, then their text answers.
5. Findings, each with the evidence and, where possible, a one-click fix that only makes rules stricter:
   - silent defaults: things the owner never decided, filled in by defaults;
   - reality not covered: what the agent does that no rule governs (a category with no budget...);
   - written but not enforced: a policy section with a limit or approval that no rule node connects
     to (an isolated region of the graph);
   - conflicts: a policy document and the mandate disagree about the same limit.
6. Owner knowledge. Much of what matters is never written down. When the owner marks a finding as
   intended and says why, that note is kept and used in every later check.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from .agent import CATEGORIES
from .llm import LLM, LLMError
from .models import Mandate

STOPWORDS = {"the", "and", "are", "for", "what", "who", "how", "does", "can", "our", "any", "with", "about", "rules", "rule", "policy", "say", "says", "need", "needs", "there", "this", "that", "when", "which", "may", "must"}
CMAX_WORDS = 60  # policy sections are short; a section that fits is never cut
OVERLAP = 0.1
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.M)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
MONEY = re.compile(r"\$\s?([0-9][0-9,]*(?:\.[0-9]{1,2})?)")
DOMAIN = re.compile(r"(?<![@\w.-])((?:[a-z0-9-]+\.)+(?:com|net|org|io|co|example\.com))\b", re.I)

CATEGORY_WORDS = {
    "Contractors": ["contractor", "freelancer", "illustrator", "editor", "designer"],
    "Client billing": ["invoice", "client", "billing"],
    "Stock and software": ["software", "subscription", "stock", "licence", "license", "vendor", "supplier"],
    "Marketing": ["marketing", "advertising", "ads", "promotion", "sponsorship"],
    "Travel": ["travel", "flight", "hotel", "taxi", "train"],
    "Refunds": ["refund", "chargeback"],
}
CONCEPTS = {
    "per_payment_cap": ["per payment", "single payment", "one payment", "any payment", "each payment", "payment cap"],
    "weekly_budget": ["per week", "a week", "weekly", "each week", "7 days"],
    "daily_budget": ["per day", "a day", "daily", "each day"],
    "approval": ["approval", "approve", "sign off", "sign-off", "signature", "two people", "second person", "two approvals"],
    "working_hours": ["weekend", "working hours", "office hours", "after hours", "night", "business hours"],
    "new_payee": ["new payee", "new supplier", "new vendor", "first payment", "never paid"],
    "refunds": ["refund"],
    "invoice_cap": ["invoice over", "invoices over", "largest invoice", "invoice cap"],
}

SUMMARY_SYSTEM = """You write the summary chunk of a business policy document so a search system can find every part of it.
Reply with one JSON object and nothing else:
{"overview": "3 to 5 sentences: the document's scope, the main topics, and how the sections depend on each other",
 "sections": [{"title": "exact section title", "summary": "one sentence", "keywords": ["5 to 10 terms copied exactly from that section: names, amounts, categories, conditions"]}]}
Keywords must appear word for word in the section. Add nothing outside this structure."""

ANSWER_SYSTEM = """You answer a business owner's question about their own spending rules, using only the policy sections and enforced rules given.
Reply with one JSON object: {"answer": "plain words, at most four sentences", "cites": ["section titles or rule names you used"]}
If the sections do not answer it, say so."""


# ---- 1. structure-preserving chunking --------------------------------------------------------------

def _words(text: str) -> int:
    return len(text.split())


def _sections(text: str, level: int) -> list[tuple[str, str]]:
    """Split text at headings of exactly this level. Returns (title, body including the heading)."""
    marks = [m for m in HEADING.finditer(text) if len(m.group(1)) == level]
    if not marks:
        return []
    parts = []
    if marks[0].start() > 0 and text[: marks[0].start()].strip():
        parts.append(("Introduction", text[: marks[0].start()].strip()))
    for k, m in enumerate(marks):
        end = marks[k + 1].start() if k + 1 < len(marks) else len(text)
        parts.append((m.group(2).strip(), text[m.start():end].strip()))
    return parts


def _by_size(text: str, cmax: int, overlap: float) -> list[str]:
    words = text.split()
    step = max(1, int(cmax * (1 - overlap)))
    return [" ".join(words[i:i + cmax]) for i in range(0, len(words), step) if words[i:i + cmax]]


def chunk(text: str, cmax: int = CMAX_WORDS, overlap: float = OVERLAP, path: tuple[str, ...] = (), level: int = 1) -> list[dict[str, Any]]:
    """Split along the document's own headings, as large as fits, deeper only where needed."""
    text = text.strip()
    if not text:
        return []
    if _words(text) <= cmax:
        first = HEADING.search(text)
        title = path[-1] if path else (first.group(2).strip() if first else "Document")
        return [{"path": list(path) or [title], "title": title, "text": text}]
    for depth in range(level, 7):
        parts = _sections(text, depth)
        if len(parts) == 1 and parts[0][0]:
            # One heading over everything (the document title): keep it in the path and look inside.
            title, body = parts[0]
            inner = body.split("\n", 1)[1] if "\n" in body else ""
            return chunk(inner, cmax, overlap, path + (title,), depth + 1)
        if len(parts) > 1:
            out = []
            for title, body in parts:
                out.extend(chunk(body, cmax, overlap, path + ((title,) if title else ()), depth + 1))
            return out
    return [{"path": list(path), "title": (path[-1] if path else "Document") + f" (part {k + 1})", "text": piece}
            for k, piece in enumerate(_by_size(text, cmax, overlap))]


# ---- 2. the summary chunk --------------------------------------------------------------------------

def _units(chunks: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Every headed section, titled by its own heading, with its own text."""
    out: list[tuple[str, str]] = []
    for c in chunks:
        for path, body in leaf_units(c):
            title = path[-1] if path else c["title"]
            if title not in dict(out):
                out.append((title, body))
    return out


def _fallback_summary(name: str, chunks: list[dict[str, Any]]) -> dict[str, Any]:
    sections = []
    for title, text in _units(chunks):
        body = HEADING.sub("", text).strip()
        first = re.split(r"(?<=[.!?])\s", body, maxsplit=1)[0][:200]
        sections.append({"title": title, "summary": first, "keywords": sorted(entity_names(text))[:10]})
    return {"overview": f"{name} covers: " + ", ".join(s["title"] for s in sections) + ".", "sections": sections}


async def summarise(llm: Optional[LLM], name: str, chunks: list[dict[str, Any]]) -> dict[str, Any]:
    if llm is None:
        return _fallback_summary(name, chunks)
    outline = "\n".join(f"## {title}\n{text[:1500]}" for title, text in _units(chunks))
    try:
        raw = await llm.json(SUMMARY_SYSTEM, f"DOCUMENT: {name}\nSECTIONS:\n{outline}")
    except LLMError:
        return _fallback_summary(name, chunks)
    by_title = {title: text.lower() for title, text in _units(chunks)}
    sections = []
    for s in raw.get("sections") or []:
        title = str(s.get("title", "")).strip()
        body = by_title.get(title)
        if body is None:
            continue
        keywords = [k for k in (str(x).strip() for x in s.get("keywords") or []) if k and k.lower() in body][:10]
        sections.append({"title": title, "summary": str(s.get("summary", ""))[:300], "keywords": keywords})
    covered = {s["title"] for s in sections}
    sections += [s for s in _fallback_summary(name, chunks)["sections"] if s["title"] not in covered]
    return {"overview": str(raw.get("overview", ""))[:1200] or _fallback_summary(name, chunks)["overview"], "sections": sections}


async def ingest(llm: Optional[LLM], name: str, text: str) -> dict[str, Any]:
    chunks = chunk(text)
    return {"name": name[:120], "text": text, "chunks": chunks, "summary": await summarise(llm, name, chunks)}


# ---- 3. entities and the knowledge graph -------------------------------------------------------------

def entities(text: str) -> set[tuple[str, str]]:
    lowered = text.lower()
    found: set[tuple[str, str]] = set()
    for email in EMAIL.findall(text):
        found.add(("payee", email.lower()))
        found.add(("domain", email.lower().rsplit("@", 1)[-1]))
    for domain in DOMAIN.findall(text):
        found.add(("domain", domain.lower()))
    for amount in MONEY.findall(text):
        found.add(("amount", f"{float(amount.replace(',', '')):.2f}"))
    for category, words in CATEGORY_WORDS.items():
        if category.lower() in lowered or any(re.search(rf"\b{re.escape(w)}", lowered) for w in words):
            found.add(("category", category))
    for concept, words in CONCEPTS.items():
        if any(w in lowered for w in words):
            found.add(("concept", concept))
    return found


def entity_names(text: str) -> set[str]:
    return {value for _, value in entities(text)}


def _rule_entities(mandate: Mandate) -> dict[str, set[tuple[str, str]]]:
    """Enforced rules as graph nodes, each joined to the entities it governs."""
    rules: dict[str, set[tuple[str, str]]] = {
        "per_payment_cap": {("concept", "per_payment_cap"), ("amount", f"{mandate.per_payment_cap:.2f}")},
        "auto_approve_limit": {("concept", "approval"), ("amount", f"{mandate.auto_approve_limit:.2f}")},
        "allowed_payees": {("payee", e) for e in mandate.allowed_payees} | {("domain", e.rsplit("@", 1)[-1]) for e in mandate.allowed_payees},
        "allowed_domains": {("domain", d) for d in mandate.allowed_domains},
        "blocked_payees": {("payee", e) for e in mandate.blocked_payees},
        "category_budgets": {("category", c) for c in mandate.category_budgets} | {("concept", "weekly_budget")} if mandate.category_budgets else set(),
        "new_payee_cap": {("concept", "new_payee")} if mandate.new_payee_cap is not None else set(),
        "invoice_cap": {("concept", "invoice_cap"), ("category", "Client billing")} if mandate.invoice_cap is not None else set(),
    }
    if mandate.weekly_budget is not None:
        rules["weekly_budget"] = {("concept", "weekly_budget"), ("amount", f"{mandate.weekly_budget:.2f}")}
    if mandate.daily_budget is not None:
        rules["daily_budget"] = {("concept", "daily_budget"), ("amount", f"{mandate.daily_budget:.2f}")}
    if mandate.dual_control_over is not None:
        rules["dual_control_over"] = {("concept", "approval"), ("amount", f"{mandate.dual_control_over:.2f}")}
    if mandate.active_hours:
        rules["active_hours"] = {("concept", "working_hours")}
    return rules


def build_graph(mandate: Mandate, docs: list[dict[str, Any]], payments: list[dict[str, Any]]) -> dict[str, Any]:
    nodes: dict[str, dict[str, Any]] = {}
    edges: set[tuple[str, str, str]] = set()

    def node(key: str, kind: str, label: str) -> str:
        nodes.setdefault(key, {"id": key, "kind": kind, "label": label})
        return key

    for kind, value in entities(mandate.text):
        edges.add((node("mandate:text", "mandate", "Your mandate"), node(f"{kind}:{value}", kind, value), "mentions"))
    for rule, ents in _rule_entities(mandate).items():
        r = node(f"rule:{rule}", "rule", rule)
        for kind, value in ents:
            edges.add((r, node(f"{kind}:{value}", kind, value), "governs"))
    for d in docs:
        doc_id = node(f"doc:{d['id']}", "document", d["name"])
        summary = node(f"summary:{d['id']}", "summary", f"Summary of {d['name']}")
        edges.add((doc_id, summary, "summarised by"))
        for k, c in enumerate(d["chunks"]):
            sec = node(f"section:{d['id']}:{k}", "section", " > ".join(c["path"]) or c["title"])
            edges.add((summary, sec, "indexes"))
            for kind, value in entities(c["text"]):
                edges.add((sec, node(f"{kind}:{value}", kind, value), "mentions"))
        for s in d["summary"]["sections"]:
            for kw in s["keywords"]:
                for kind, value in entities(kw):
                    edges.add((summary, node(f"{kind}:{value}", kind, value), "mentions"))
        for kind, value in entities(d["summary"]["overview"]):
            edges.add((summary, node(f"{kind}:{value}", kind, value), "links"))
    for p in payments:
        if p["status"] not in ("executed", "pending_approval", "awaiting_client", "scheduled"):
            continue
        pay = node(f"payment:{p['id']}", "payment", f"{p['counterparty_name'] or p['counterparty_email']} {p['amount']:.2f}")
        edges.add((pay, node(f"payee:{p['counterparty_email']}", "payee", p["counterparty_email"]), "to"))
        if p.get("category"):
            edges.add((pay, node(f"category:{p['category']}", "category", p["category"]), "in"))
    return {"nodes": nodes, "edges": sorted(edges)}


def leaf_units(c: dict[str, Any]) -> list[tuple[list[str], str]]:
    """The smallest headed sections inside a chunk, each with its full heading path."""
    marks = list(HEADING.finditer(c["text"]))
    if not marks:
        return [(c["path"] or [c["title"]], c["text"])]
    units, base = [], list(c["path"])
    stack: list[tuple[int, str]] = []
    head = c["text"][: marks[0].start()].strip()
    if head:
        units.append((base or [c["title"]], head))
    for n, m in enumerate(marks):
        level, title = len(m.group(1)), m.group(2).strip()
        stack = [x for x in stack if x[0] < level] + [(level, title)]
        body = c["text"][m.end(): marks[n + 1].start() if n + 1 < len(marks) else len(c["text"])].strip()
        path = base + [t for _, t in stack if not base or t != base[-1]]
        if body:
            units.append((path, body))
    return units


def _neighbours(graph: dict[str, Any], key: str) -> set[str]:
    out = set()
    for a, b, _ in graph["edges"]:
        if a == key:
            out.add(b)
        elif b == key:
            out.add(a)
    return out


# ---- 5. findings -------------------------------------------------------------------------------------

def _finding(fid: str, kind: str, title: str, detail: str, evidence: list[str], fix: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    return {"id": fid, "kind": kind, "title": title, "detail": detail, "evidence": evidence, "fix": fix}


def _limit_in(text: str, concept: str) -> Optional[float]:
    """The amount stated next to a concept in a sentence, for example 'no single payment over $400'."""
    for sentence in re.split(r"(?<=[.!?\n])\s+", text):
        low = sentence.lower()
        if any(w in low for w in CONCEPTS[concept]):
            amounts = MONEY.findall(sentence)
            if amounts:
                return float(amounts[0].replace(",", ""))
    return None


def check(mandate: Mandate, docs: list[dict[str, Any]], payments: list[dict[str, Any]], knowledge: dict[str, str]) -> dict[str, Any]:
    graph = build_graph(mandate, docs, payments)
    text = mandate.text.lower()
    findings: list[dict[str, Any]] = []

    # Global pass: which areas exist anywhere, and which are governed by a rule.
    governed = {b for a, b, rel in graph["edges"] if rel == "governs"}
    said = entities(mandate.text)

    # Silent defaults: decisions the owner never made.
    silent = [
        ("active_hours", not mandate.active_hours and not any(w in text for w in CONCEPTS["working_hours"]),
         "No working hours", "The mandate never says when the agent may pay, so it can pay at 3 am or on a Sunday.",
         {"field": "active_hours", "value": "08:00-20:00"}),
        ("daily_budget", mandate.daily_budget is None and "day" not in text,
         "No daily limit", "Only a weekly limit applies, so the whole week's budget could go in one day.",
         {"field": "daily_budget", "value": round((mandate.weekly_budget or mandate.per_payment_cap * 3) / 2, 2)}),
        ("dual_control", mandate.dual_control_over is None and not any(w in text for w in ("two people", "second person", "two signatures")),
         "One signature is enough for any amount", "Nothing large ever needs a second person to sign.",
         {"field": "dual_control_over", "value": mandate.per_payment_cap}),
        ("expires_on", mandate.expires_on is None, "The mandate never ends", "Authority that never expires is never reviewed.", None),
    ]
    for fid, applies, title, detail, fix in silent:
        if applies:
            findings.append(_finding(f"silent:{fid}", "Silent default", title, detail, ["Your mandate"], fix))

    # Reality not covered: what the agent does that no rule governs.
    used_categories = sorted({p["category"] for p in payments if p.get("category") and p["rail"] != "invoice"
                              and p["status"] in ("executed", "pending_approval", "awaiting_client", "scheduled")})
    for cat in used_categories:
        if ("category:" + cat) not in governed:
            spent = sum(p["amount"] for p in payments if p.get("category") == cat and p["status"] == "executed")
            findings.append(_finding(f"reality:category:{cat}", "Not covered", f"{cat} has no budget",
                                     f"The agent has asked to spend on {cat} (${spent:,.2f} sent so far) but no category budget limits it.",
                                     [f"Ledger: {cat}"], {"field": "category_budgets", "key": cat, "value": max(100.0, round(spent * 1.5, -1))}))
    for p in payments:
        if p["rail"] == "purchase" and p["status"] in ("pending_approval", "executed") and not mandate.payee_approved(p["counterparty_email"]):
            domain = p["counterparty_email"].rsplit("@", 1)[-1]
            findings.append(_finding(f"reality:vendor:{domain}", "Not covered", f"{p['counterparty_name'] or domain} is on no list",
                                     f"The agent buys from {p['counterparty_email']}, which is neither approved nor blocked, so every purchase waits for you.",
                                     [f"Ledger: {p['id']}"], {"field": "allowed_domains", "value": domain}))

    # Local pass: read every leaf section of every chunk in detail.
    for d in docs:
        units = [(f"{n}.{u}", path, body, chunk_) for n, chunk_ in enumerate(d["chunks"]) for u, (path, body) in enumerate(leaf_units(chunk_))]
        firsts = {tuple(path[:1]) for _, path, _, _ in units if len(path) > 1}
        if len(firsts) == 1:  # every section sits under the document's own title: leave it out of the reference
            units = [(k, path[1:] if len(path) > 1 else path, body, ch) for k, path, body, ch in units]
        for k, path, body, chunk_ in units:
            ents = {f"{kind}:{value}" for kind, value in entities(body)}
            where = f"{d['name']} > {' > '.join(path)}"
            low = body.lower()
            c = {**chunk_, "text": body, "title": path[-1] if path else chunk_["title"]}
            # Conflicts: the document and the mandate state different limits for the same thing.
            scoped = any(n.startswith("category:") for n in ents)
            for concept, field, current in (("per_payment_cap", "per_payment_cap", mandate.per_payment_cap),
                                            ("weekly_budget", "weekly_budget", mandate.weekly_budget),
                                            ("daily_budget", "daily_budget", mandate.daily_budget)):
                stated = _limit_in(c["text"], concept)
                if stated is None or (scoped and concept != "per_payment_cap"):
                    continue  # a limit for one category is a category budget, handled below
                if current is None or abs(stated - current) > 0.005:
                    label = field.replace("_", " ").capitalize()
                    rules_say = "nothing" if current is None else f"${current:,.2f}"
                    stricter = stated if current is None else min(stated, current)
                    fix = {"field": field, "value": stricter} if current is None or stated < current else None
                    findings.append(_finding(f"conflict:{d['id']}:{k}:{field}", "Conflict",
                                             f"{label}: policy says ${stated:,.2f}, rules say {rules_say}",
                                             f"\"{where}\" and your mandate disagree. Countersign enforces the mandate, so the policy is not being followed.",
                                             [where, "Your mandate"], fix))
            # Written but not enforced: a section with a limit or an approval that no rule connects to.
            has_condition = any(n in ents for n in ("concept:approval", "concept:working_hours", "concept:new_payee", "concept:refunds")) or MONEY.search(c["text"])
            categories = [n.split(":", 1)[1] for n in ents if n.startswith("category:")]
            reached = [n for n in ents if n in governed]
            amounts = [float(a.replace(",", "")) for a in MONEY.findall(c["text"])]
            approval = "concept:approval" in ents
            weekly = "concept:weekly_budget" in ents
            for cat in categories:
                if cat == "Client billing" or ("category:" + cat) in governed or not amounts:
                    continue
                verb = "need" if cat.endswith("s") else "needs"
                if approval and "two" in low:
                    fix = {"field": "dual_control_over", "value": min(amounts[0], mandate.dual_control_over or amounts[0]),
                           "note": "There is no per-category approval rule, so this applies to every payment."}
                    detail = f"{verb} two approvals over ${amounts[0]:,.2f}"
                elif approval:
                    fix = None
                    detail = f"{verb} your approval over ${amounts[0]:,.2f}; lower your sign-off threshold or keep it in mind when signing"
                else:
                    fix = {"field": "category_budgets", "key": cat, "value": amounts[0]}
                    detail = f"{'are' if cat.endswith('s') else 'is'} limited to ${amounts[0]:,.2f}" + (" a week" if weekly else "")
                findings.append(_finding(f"unenforced:{d['id']}:{k}:{cat}", "Written, not enforced",
                                         f"{cat} {detail.split(';')[0]}: not enforced",
                                         f"\"{where}\" says {cat} {detail}, but no Countersign rule governs {cat}, so the agent is never held to it.",
                                         [where], fix))
            loose_domains = [n.split(":", 1)[1] for n in ents if n.startswith("domain:") and n not in governed]
            if loose_domains and any(w in low for w in ("approved", "only")):
                findings.append(_finding(f"unenforced:{d['id']}:{k}:domains", "Written, not enforced",
                                         f"Approved vendors are written, not enforced",
                                         f"\"{where}\" names {', '.join(loose_domains)} as approved, but your rules do not, so purchases from it wait for you and others are not refused.",
                                         [where], {"field": "allowed_domains", "value": loose_domains[0]}))
            has_condition = approval or "concept:working_hours" in ents or "concept:new_payee" in ents or "concept:refunds" in ents or bool(amounts)
            if "concept:working_hours" in ents and "concept:working_hours" not in governed:
                findings.append(_finding(f"unenforced:{d['id']}:{k}:hours", "Written, not enforced", "Working hours are written, not enforced",
                                         f"\"{where}\" limits when money may move, but the agent may pay at any time.",
                                         [where], {"field": "active_hours", "value": "08:00-20:00"}))
            if has_condition and not reached and not categories and not loose_domains and "concept:working_hours" not in ents:
                findings.append(_finding(f"isolated:{d['id']}:{k}", "Written, not enforced", f"\"{c['title']}\" connects to no rule",
                                         f"\"{where}\" states a condition, but nothing in the enforced rules or the ledger links to it. Read it and decide whether it needs a rule.",
                                         [where], None))

    # A rule the owner wrote down outranks the same gap found as a silent default.
    written = {f["fix"]["field"] for f in findings if f["fix"] and f["kind"] != "Silent default"}
    findings = [f for f in findings if not (f["kind"] == "Silent default" and f["fix"] and f["fix"]["field"] in written)]

    # Owner knowledge: findings the owner has marked as intended stay visible but settled.
    seen: dict[str, dict[str, Any]] = {}
    for f in findings:
        if f["id"] in seen:
            continue
        f["intended"] = knowledge.get(f["id"])
        seen[f["id"]] = f
    findings = list(seen.values())
    order = {"Conflict": 0, "Written, not enforced": 1, "Not covered": 2, "Silent default": 3}
    findings.sort(key=lambda f: (bool(f["intended"]), order[f["kind"]], f["id"]))
    stats = {
        "documents": len(docs), "sections": sum(len(d["chunks"]) for d in docs),
        "nodes": len(graph["nodes"]), "edges": len(graph["edges"]),
        "open": sum(1 for f in findings if not f["intended"]), "intended": sum(1 for f in findings if f["intended"]),
        "said_in_mandate": len(said),
    }
    return {"findings": findings, "stats": stats}


def apply_fix(mandate: Mandate, fix: dict[str, Any]) -> Mandate:
    """Apply a suggested fix. Every fix only adds a rule or makes one stricter."""
    data = mandate.model_dump()
    field, value = fix["field"], fix.get("value")
    if field == "category_budgets":
        budgets = dict(data["category_budgets"])
        current = budgets.get(fix["key"])
        budgets[fix["key"]] = value if current is None else min(current, value)
        data["category_budgets"] = budgets
    elif field == "allowed_domains":
        if value not in data["allowed_domains"]:
            data["allowed_domains"] = [*data["allowed_domains"], value]
    elif field in ("per_payment_cap", "weekly_budget", "daily_budget", "dual_control_over"):
        current = data.get(field)
        data[field] = value if current is None else min(current, value)
        if field == "per_payment_cap" and data["auto_approve_limit"] > data["per_payment_cap"]:
            data["auto_approve_limit"] = data["per_payment_cap"]
    elif field == "active_hours":
        data[field] = data[field] or value
    else:
        raise ValueError("This fix cannot be applied automatically.")
    return Mandate.model_validate(data)


# ---- 4. global then local questions ---------------------------------------------------------------

def retrieve(question: str, docs: list[dict[str, Any]], limit: int = 4) -> list[dict[str, Any]]:
    """Global: match the question to section summaries and keywords. Local: return those sections' text,
    plus sections that share an entity with them (one hop in the graph)."""
    q = question.lower()
    q_words = set(re.findall(r"[a-z]{3,}", q)) - STOPWORDS
    q_ents = entities(question)
    scored = []
    for d in docs:
        index = {s["title"]: s for s in d["summary"]["sections"]}
        for k, c in enumerate(d["chunks"]):
            titles = [path[-1] for path, _ in leaf_units(c) if path] or [c["title"]]
            entries = [index.get(t, {"summary": "", "keywords": []}) for t in titles]
            hay = " ".join(titles + [e["summary"] + " " + " ".join(e["keywords"]) for e in entries]).lower()
            score = sum(2 for w in q_words if w in hay) + 3 * len(q_ents & entities(c["text"]))
            if score:
                scored.append((score, d, k, c))
    scored.sort(key=lambda x: (-x[0], x[1]["id"], x[2]))
    picked = scored[:limit]
    seeds = set().union(*(entities(c["text"]) for _, _, _, c in picked)) if picked else set()
    seeds = {e for e in seeds if e[0] in ("category", "payee", "domain")}
    extra = []
    for d in docs:
        for k, c in enumerate(d["chunks"]):
            if any(k == pk and d["id"] == pd["id"] for _, pd, pk, _ in picked):
                continue
            if seeds & entities(c["text"]):
                extra.append((0, d, k, c))
    return [{"document": d["name"], "section": " > ".join(c["path"]) or c["title"], "text": c["text"]}
            for _, d, k, c in (picked + extra)[: limit + 2]]


async def answer(llm: LLM, question: str, mandate: Mandate, docs: list[dict[str, Any]]) -> dict[str, Any]:
    question = question.strip()[:500]
    if not question:
        raise LLMError("Type a question first.")
    found = retrieve(question, docs)
    overviews = "\n".join(f"{d['name']}: {d['summary']['overview']}" for d in docs)
    rules = json.dumps({k: v for k, v in mandate.model_dump(mode="json").items() if k not in ("text",)}, default=str)
    context = "\n\n".join(f"[{f['document']} > {f['section']}]\n{f['text']}" for f in found)
    raw = await llm.json(ANSWER_SYSTEM, f"OVERVIEW\n{overviews}\n\nENFORCED RULES\n{rules}\n\nSECTIONS\n{context}\n\nQUESTION\n{question}")
    allowed = {f["section"] for f in found} | {f"{f['document']} > {f['section']}" for f in found} | set(Mandate.model_fields)
    cites = [c for c in (str(x).strip() for x in raw.get("cites") or []) if c in allowed][:6]
    return {"question": question, "answer": str(raw.get("answer", ""))[:1200], "cites": cites,
            "sections": [{"document": f["document"], "section": f["section"]} for f in found]}


SAMPLE_POLICY = """# Kestrel Studio spending policy

This policy covers how the studio pays people and buys things. Section 2 sets the general limits,
section 3 adds rules per kind of spending, and section 4 covers who must approve what.

## 1. Who we pay

We pay contractors who have a signed agreement with the studio. A new contractor's first payment
must be confirmed by phone before it is sent.

## 2. General limits

No single payment over $400. The studio does not spend more than $1,500 in a week.

## 3. Kinds of spending

### Marketing

Marketing and advertising spend is limited to $250 per week, and every new campaign is agreed in advance.

### Travel

Travel is booked only for client work. Any travel over $300 needs two approvals.

### Software and stock

Software subscriptions and stock assets are bought from approved vendors only, for example lumenstock.example.com.

## 4. Approvals and timing

No payments are made at weekends or outside office hours. Refunds to clients over $100 need the owner's approval.
"""
