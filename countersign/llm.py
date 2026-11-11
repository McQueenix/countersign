"""A small client for any OpenAI-compatible chat completions endpoint.

Works with Gemini, Groq, OpenAI, OpenRouter, Ollama and others: set
LLM_BASE_URL, LLM_API_KEY and LLM_MODEL.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Optional

import httpx


class LLMError(Exception):
    pass


RETRY_WAITS = (3.0, 8.0, 20.0, 30.0)


def extract_json(text: str) -> dict[str, Any]:
    """Pull the first JSON object out of a model reply, tolerating code fences and stray prose."""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        value = json.loads(text[start : index + 1])
                        if isinstance(value, dict):
                            return value
                    except json.JSONDecodeError:
                        pass
                    break
        start = text.find("{", start + 1)
    raise LLMError("The model did not return a JSON object.")


class LLM:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 60.0,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self._http = httpx.AsyncClient(timeout=timeout, transport=transport)

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.model)

    async def close(self) -> None:
        await self._http.aclose()

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.configured:
            raise LLMError("No AI model is configured. Set LLM_API_KEY and LLM_MODEL in .env.")
        response = None
        # Free tiers rate-limit hard. Wait and retry rather than fail a payment review.
        for wait in (*RETRY_WAITS, None):
            try:
                response = await self._http.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                    json=payload,
                )
            except httpx.HTTPError as exc:
                raise LLMError(f"Could not reach the AI model: {exc.__class__.__name__}") from exc
            if response.status_code not in (429, 500, 502, 503, 529) or wait is None:
                break
            try:
                wait = min(30.0, max(wait, float(response.headers.get("retry-after", wait))))
            except ValueError:
                pass
            await asyncio.sleep(wait)
        if response.status_code >= 400:
            raise LLMError(f"AI model returned {response.status_code}: {response.text[:300]}")
        try:
            return response.json()["choices"][0]["message"]
        except (KeyError, IndexError, ValueError) as exc:
            raise LLMError("AI model returned an unexpected response shape.") from exc

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        temperature: float = 0.0,
    ) -> dict[str, Any]:
        """One chat turn. Returns the assistant message, which may contain tool_calls."""
        payload: dict[str, Any] = {"model": self.model, "messages": messages, "temperature": temperature}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        return await self._post(payload)

    async def json(self, system: str, user: str) -> dict[str, Any]:
        """Ask for a JSON object. Falls back to plain text parsing if JSON mode is not supported."""
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
        }
        try:
            message = await self._post(payload)
        except LLMError as exc:
            if " 400" not in str(exc) and " 422" not in str(exc):
                raise
            payload.pop("response_format")
            message = await self._post(payload)
        return extract_json(message.get("content") or "")

    async def ping(self) -> None:
        await self.chat([{"role": "user", "content": "Reply with the single word: ok"}])
