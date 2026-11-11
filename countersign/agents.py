"""Agent identities: each outside agent gets its own key and weekly allowance from the owner.

Keys are shown once and stored only as SHA-256 hashes. Revoking an agent takes effect on its next request.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from typing import Any, Optional

from .store import Store, now_iso

KEY = "agents"


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


class AgentRegistry:
    def __init__(self, store: Store) -> None:
        self.store = store

    def _load(self) -> list[dict[str, Any]]:
        return json.loads(self.store.get_kv(KEY) or "[]")

    def _save(self, agents: list[dict[str, Any]]) -> None:
        self.store.set_kv(KEY, json.dumps(agents))

    def list(self) -> list[dict[str, Any]]:
        return [{k: v for k, v in a.items() if k != "key_hash"} for a in self._load()]

    def create(self, name: str, weekly_allowance: Optional[float]) -> tuple[dict[str, Any], str]:
        name = " ".join(name.split())[:40]
        if not name:
            raise ValueError("Give the agent a name.")
        agents = self._load()
        if any(a["name"].lower() == name.lower() and a["active"] for a in agents):
            raise ValueError("An active agent already has that name.")
        key = "csk_" + secrets.token_urlsafe(24)
        agent = {"id": "ag_" + uuid.uuid4().hex[:8], "name": name, "weekly_allowance": weekly_allowance,
                 "active": True, "created_at": now_iso(), "key_hash": _hash(key), "key_hint": key[-4:]}
        agents.append(agent)
        self._save(agents)
        self.store.audit("agent_registered", "owner", {"agent": name, "weekly_allowance": weekly_allowance})
        return {k: v for k, v in agent.items() if k != "key_hash"}, key

    def revoke(self, agent_id: str) -> dict[str, Any]:
        agents = self._load()
        for agent in agents:
            if agent["id"] == agent_id:
                agent["active"] = False
                self._save(agents)
                self.store.audit("agent_revoked", "owner", {"agent": agent["name"]})
                return {k: v for k, v in agent.items() if k != "key_hash"}
        raise KeyError(agent_id)

    def by_key(self, key: str) -> Optional[dict[str, Any]]:
        digest = _hash(key.strip())
        for agent in self._load():
            if secrets.compare_digest(agent["key_hash"], digest):
                return agent
        return None
