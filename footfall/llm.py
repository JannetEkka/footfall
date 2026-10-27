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
DEFAULT_MAX_TOKENS = 1800
# The cheapest Nemotrons on Token Factory (GET /v1/models?verbose=true, 2026-10-27)
# are Nano 30B-A3B and 3.5 Lightning, both $0.06/M in and $0.24/M out, both with
# tool calling. On the same tool-call test Lightning answered in 1.6 s with 118
# tokens, Nano in 7.4 s with 483 (hidden reasoning), so Lightning is the default.
NEBIUS_DEFAULT_MODEL = "nvidia/Nemotron-3_5-Lightning"
# The write-up is where quality shows. On the same replayed Qloo data (10-27),
# Lightning writing kept 2 of 5 Brooklyn suggestions (it listed tags as books)
# and misread the taste gap in Shoreditch; Super 120B ($0.30/M in, $0.90/M out)
# kept 4/4 and 5/5 and read the gap right, for about $0.005 more per run. Ultra
# 550B was more detailed but leaked IDs into the text at 2-3x Super's cost.
NEBIUS_DEFAULT_WRITE_MODEL = "nvidia/nemotron-3-super-120b-a12b"


@dataclass
class Reply:
    content: str
    tool_calls: list[dict] = field(default_factory=list)  # [{"id", "name", "arguments": dict}]
    usage: dict = field(default_factory=dict)
    finish_reason: str | None = None


class LLMError(RuntimeError):
    pass


class ChatLLM:
    def __init__(self, base_url: str, api_key: str, model: str, *, transport: httpx.AsyncBaseTransport | None = None,
                 label: str = "", no_think: dict | None = None, write_model: str | None = None):
        self.model = model
        self.write_model = write_model or model  # the model for the final write-up, if different
        self.no_think = no_think  # extra request fields that switch reasoning off, if the server has them
        self.label = label or model
        self._http = httpx.AsyncClient(base_url=base_url.rstrip("/") + "/", transport=transport, timeout=90,
                                       headers={"Authorization": f"Bearer {api_key}"})
        self.configured = bool(api_key)
        self.tokens = {"prompt": 0, "completion": 0}

    async def chat(self, messages: list[dict], tools: list[dict] | None = None, *, temperature: float = 0.3,
                   max_tokens: int = DEFAULT_MAX_TOKENS, json_mode: bool = False, think: bool = True,
                   model: str | None = None) -> Reply:
        if not self.configured:
            raise LLMError("no API key for the LLM provider")
        body: dict[str, Any] = {"model": model or self.model, "messages": messages, "temperature": temperature,
                                "max_tokens": max_tokens}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        if not think and self.no_think:
            body.update(self.no_think)
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
        return Reply(content=msg.get("content") or "", tool_calls=calls, usage=u,
                     finish_reason=data["choices"][0].get("finish_reason"))

    async def aclose(self) -> None:
        await self._http.aclose()


class ScriptedLLM:
    """Replays a list of replies (or a function of the messages). Tests only."""

    label = "scripted"
    configured = True
    model = "scripted"
    write_model = "scripted"

    def __init__(self, script: list[Reply] | Callable[[list[dict], list[dict] | None], Reply]):
        self.script = script
        self.seen: list[list[dict]] = []
        self.options: list[dict] = []  # the keyword arguments of each call
        self.tokens = {"prompt": 0, "completion": 0}

    async def chat(self, messages, tools=None, **kw) -> Reply:
        self.seen.append(list(messages))
        self.options.append(kw)
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
        write = os.environ.get("NEBIUS_WRITE_MODEL", NEBIUS_DEFAULT_WRITE_MODEL)
        label = (f"{model} on Nebius Token Factory" if write == model else
                 f"{model} for the Qloo calls and {write} for the write-up, on Nebius Token Factory")
        return ChatLLM(NEBIUS_BASE, os.environ.get("NEBIUS_HACK_API_KEY", ""), model, label=label,
                       no_think={"chat_template_kwargs": {"enable_thinking": False}}, write_model=write)
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
