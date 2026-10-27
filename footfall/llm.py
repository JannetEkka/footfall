"""LLM provider switch. Everything goes through one OpenAI-compatible
/chat/completions call, so the agent doesn't care which provider is behind it.

LLM_PROVIDER:
  nebius   (default) NVIDIA Nemotron on Nebius Token Factory. Key: NEBIUS_HACK_API_KEY.
  openai   any other OpenAI-compatible server: LLM_BASE_URL, LLM_API_KEY, LLM_MODEL.
  scripted canned replies, for tests only.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

NEBIUS_BASE = "https://api.tokenfactory.nebius.com/v1"
# Unverified until the key exists: check GET /v1/models and set NEBIUS_MODEL.
NEBIUS_DEFAULT_MODEL = "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"


@dataclass
class Reply:
    content: str
    tool_calls: list[dict] = field(default_factory=list)  # [{"id", "name", "arguments": dict}]
    usage: dict = field(default_factory=dict)


class LLMError(RuntimeError):
    pass


class ChatLLM:
    def __init__(self, base_url: str, api_key: str, model: str, *, transport: httpx.AsyncBaseTransport | None = None,
                 label: str = ""):
        self.model = model
        self.label = label or model
        self._http = httpx.AsyncClient(base_url=base_url.rstrip("/") + "/", transport=transport, timeout=90,
                                       headers={"Authorization": f"Bearer {api_key}"})
        self.configured = bool(api_key)
        self.tokens = {"prompt": 0, "completion": 0}

    async def chat(self, messages: list[dict], tools: list[dict] | None = None, *, temperature: float = 0.3,
                   max_tokens: int = 1800, json_mode: bool = False) -> Reply:
        if not self.configured:
            raise LLMError("no API key for the LLM provider")
        body: dict[str, Any] = {"model": self.model, "messages": messages, "temperature": temperature,
                                "max_tokens": max_tokens}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        r = await self._http.post("chat/completions", json=body)
        if r.status_code >= 400:
            raise LLMError(f"{r.status_code}: {r.text[:300]}")
        data = r.json()
        msg = data["choices"][0]["message"]
        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            calls.append({"id": tc.get("id") or fn.get("name"), "name": fn.get("name"), "arguments": args})
        u = data.get("usage") or {}
        self.tokens["prompt"] += u.get("prompt_tokens", 0)
        self.tokens["completion"] += u.get("completion_tokens", 0)
        return Reply(content=msg.get("content") or "", tool_calls=calls, usage=u)

    async def aclose(self) -> None:
        await self._http.aclose()


class ScriptedLLM:
    """Replays a list of replies (or a function of the messages). Tests only."""

    label = "scripted"
    configured = True
    model = "scripted"

    def __init__(self, script: list[Reply] | Callable[[list[dict], list[dict] | None], Reply]):
        self.script = script
        self.seen: list[list[dict]] = []
        self.tokens = {"prompt": 0, "completion": 0}

    async def chat(self, messages, tools=None, **kw) -> Reply:
        self.seen.append(list(messages))
        if callable(self.script):
            return self.script(messages, tools)
        if not self.script:
            raise LLMError("script exhausted")
        return self.script.pop(0)

    async def aclose(self) -> None:
        pass


def from_env() -> ChatLLM:
    provider = os.environ.get("LLM_PROVIDER", "nebius").lower()
    if provider == "nebius":
        model = os.environ.get("NEBIUS_MODEL", NEBIUS_DEFAULT_MODEL)
        return ChatLLM(NEBIUS_BASE, os.environ.get("NEBIUS_HACK_API_KEY", ""), model,
                       label=f"{model} on Nebius Token Factory")
    if provider == "openai":
        return ChatLLM(os.environ["LLM_BASE_URL"], os.environ.get("LLM_API_KEY", ""), os.environ["LLM_MODEL"])
    raise LLMError(f"unknown LLM_PROVIDER {provider!r}")


def extract_json(text: str) -> dict:
    """Pull the first JSON object out of a model reply (models wrap it in prose or fences)."""
    s = text.strip()
    if "</think>" in s:  # reasoning models may prepend their thinking
        s = s.split("</think>", 1)[1]
    start = s.find("{")
    if start < 0:
        raise ValueError("no JSON object in reply")
    depth, in_str, esc = 0, False, False
    for i in range(start, len(s)):
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return json.loads(s[start : i + 1])
    raise ValueError("unterminated JSON object in reply")
