"""Spend cap and visitor limits for the public demo.

Token Factory has no spending limit of its own, and the account has a card on
file that is charged automatically when the balance goes below zero. The demo
stays public until mid-December. So Footfall enforces a hard cap itself:

1. Prices are Token Factory's own, per model and per token, from
   GET /v1/models?verbose=true (with a dated fallback table). A model with no
   known price is refused. Every cost is multiplied by PRICE_MARGIN.
2. Before a run starts, its ceiling (FOOTFALL_RUN_CEILING_USD) is reserved in
   a durable spend ledger (store.py). If that would take the total past the cap
   (FOOTFALL_BUDGET_USD), or past the day's run limit, the run doesn't start.
3. During the run, every LLM call is checked *before* it is sent: its worst
   case (prompt bytes / 2 as tokens, plus max_tokens) must fit in what is left
   of the reservation. Afterwards it is priced from the usage Token Factory
   reports.
4. After the run, the reservation is replaced by the actual cost. If the server
   dies mid-run, the whole reservation stays counted.

So the ledger's total is never below what was spent, and never passes the cap.
On Render (RENDER is set) a ledger that isn't durable means no LLM calls at all:
an in-memory counter would reset on every restart.

Visitor limits (in memory, per IP) keep one visitor from using the day's runs.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from .llm import DEFAULT_MAX_TOKENS, LLMError, Reply
from .store import Conflict, StoreError

LEDGER = "budget/spend.json"
PRICE_MARGIN = 1.25

# USD per token (prompt, completion), from GET /v1/models?verbose=true on 2026-10-27.
FALLBACK_PRICES: dict[str, tuple[float, float]] = {
    "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B": (0.06e-6, 0.24e-6),
    "nvidia/Nemotron-3_5-Lightning": (0.06e-6, 0.24e-6),
    "nvidia/nemotron-3-super-120b-a12b": (0.3e-6, 0.9e-6),
    "nvidia/Nemotron-3-Ultra-550b-a55b": (1.0e-6, 3.0e-6),
}


class BudgetError(LLMError):
    """The spend cap, the run's ceiling or the day's limit would be passed."""


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


async def fetch_prices(base_url: str, api_key: str, transport: httpx.AsyncBaseTransport | None = None
                       ) -> dict[str, tuple[float, float]]:
    """Per-token prices from Token Factory's model list; {} if it can't be read."""
    try:
        async with httpx.AsyncClient(base_url=base_url.rstrip("/") + "/", transport=transport, timeout=20,
                                     headers={"Authorization": f"Bearer {api_key}"}) as http:
            r = await http.get("models", params={"verbose": "true"})
        out = {}
        for m in r.json().get("data") or []:
            p = m.get("pricing") or {}
            if p.get("prompt") is not None and p.get("completion") is not None:
                out[m["id"]] = (float(p["prompt"]), float(p["completion"]))
        return out
    except (httpx.HTTPError, ValueError, KeyError, AttributeError):
        return {}


@dataclass
class Prices:
    table: dict[str, tuple[float, float]]

    def cost(self, model: str, prompt_tokens: int, completion_tokens: int) -> float:
        if model not in self.table:
            raise BudgetError(f"no known price for {model}, so it isn't used")
        pin, pout = self.table[model]
        return (prompt_tokens * pin + completion_tokens * pout) * PRICE_MARGIN


@dataclass
class Reservation:
    amount: float
    spent: float = 0.0
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    settled: bool = False

    @property
    def left(self) -> float:
        return self.amount - self.spent


class Budget:
    def __init__(self, store, *, cap_usd: float, run_ceiling_usd: float, runs_per_day: int,
                 require_durable: bool = False):
        self.store = store
        self.cap = cap_usd
        self.ceiling = run_ceiling_usd
        self.runs_per_day = runs_per_day
        self.require_durable = require_durable
        self._lock = asyncio.Lock()

    @classmethod
    def from_env(cls, store) -> "Budget":
        # At ~$0.015 a run, 20 runs a day is at most ~$0.30 a day: $14.10 from Oct 29 to the end of
        # Nebius judging on Dec 15, so even constant use can't exhaust a $15 cap before judges are done.
        return cls(store, cap_usd=_env_float("FOOTFALL_BUDGET_USD", 15.0),
                   run_ceiling_usd=_env_float("FOOTFALL_RUN_CEILING_USD", 0.05),
                   runs_per_day=int(_env_float("FOOTFALL_RUNS_PER_DAY", 20)),
                   require_durable=bool(os.environ.get("RENDER")) or bool(os.environ.get("FOOTFALL_REQUIRE_DURABLE")))

    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    async def _update(self, change, message: str) -> dict:
        """Read the ledger, apply change(state) (which may raise), write it back."""
        if self.require_durable and not self.store.durable:
            raise BudgetError("the spend ledger isn't durable here (set FOOTFALL_STORE_REPO and "
                              "FOOTFALL_STORE_TOKEN), so LLM calls are off")
        async with self._lock:
            for _ in range(4):
                try:
                    state, version = await self.store.read(LEDGER)
                    state = state or {"committed_usd": 0.0, "runs": 0, "calls": 0, "prompt_tokens": 0,
                                      "completion_tokens": 0, "days": {}}
                    change(state)
                    state["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                    state["days"] = dict(sorted(state["days"].items())[-14:])
                    await self.store.write(LEDGER, state, version, message)
                    return state
                except Conflict:
                    await asyncio.sleep(0.3)
                except StoreError as e:
                    raise BudgetError(f"spend ledger unavailable ({e}), so LLM calls are off") from e
        raise BudgetError("spend ledger busy, try again in a moment")

    async def status(self, max_age: float = 30.0) -> dict:
        """For the health endpoint. Cached, so page loads can't use up the store's API
        rate limit (which would lock the demo: no ledger, no runs)."""
        cached = getattr(self, "_status", None)
        if cached and time.monotonic() - cached[0] < max_age:
            return cached[1]
        try:
            state, _ = await self.store.read(LEDGER)
        except StoreError as e:
            return {"ok": False, "error": str(e)}
        state = state or {"committed_usd": 0.0, "runs": 0, "days": {}}
        out = {"ok": True, "cap_usd": self.cap, "spent_usd": round(state["committed_usd"], 4),
               "left_usd": round(self.cap - state["committed_usd"], 4), "runs": state["runs"],
               "runs_today": state["days"].get(self._today(), 0), "runs_per_day": self.runs_per_day,
               "durable": self.store.durable}
        self._status = (time.monotonic(), out)
        return out

    async def reserve(self) -> Reservation:
        def change(state):
            today = self._today()
            if state["days"].get(today, 0) >= self.runs_per_day:
                raise BudgetError("the demo has used today's runs; please try again tomorrow")
            if state["committed_usd"] + self.ceiling > self.cap:
                raise BudgetError("the demo's LLM budget is used up")
            state["committed_usd"] += self.ceiling
            state["runs"] += 1
            state["days"][today] = state["days"].get(today, 0) + 1

        await self._update(change, "reserve a run")
        self._status = None
        return Reservation(self.ceiling)

    async def settle(self, res: Reservation) -> None:
        if res.settled:
            return

        def change(state):
            state["committed_usd"] += res.spent - res.amount
            state["calls"] += res.calls
            state["prompt_tokens"] += res.prompt_tokens
            state["completion_tokens"] += res.completion_tokens

        await self._update(change, f"settle a run: ${res.spent:.5f}")
        res.settled = True
        self._status = None


def worst_prompt_tokens(messages: list[dict], tools: list[dict] | None) -> int:
    """An over-estimate: tokenizers average 3-4 bytes per token on this kind of JSON."""
    return len(json.dumps([messages, tools or []]).encode()) // 2 + 64


class MeteredLLM:
    """Wraps a ChatLLM: refuses a call that might not fit the run's reservation,
    and prices every call from the usage Token Factory returns."""

    def __init__(self, llm, reservation: Reservation, prices: Prices):
        self.llm = llm
        self.res = reservation
        self.prices = prices
        self.label = llm.label
        self.model = llm.model
        self.write_model = getattr(llm, "write_model", llm.model)
        self.configured = llm.configured
        self.tokens = llm.tokens

    async def chat(self, messages, tools=None, **kw) -> Reply:
        max_tokens = kw.get("max_tokens", DEFAULT_MAX_TOKENS)
        model = kw.get("model") or self.model
        worst = self.prices.cost(model, worst_prompt_tokens(messages, tools), max_tokens)
        if worst > self.res.left:
            raise BudgetError("this run reached its spending ceiling")
        reply = await self.llm.chat(messages, tools, **kw)
        u = reply.usage or {}
        # Only a missing count falls back to the worst case; a reported 0 is a real 0.
        pt = int(u["prompt_tokens"]) if u.get("prompt_tokens") is not None else worst_prompt_tokens(messages, tools)
        ct = int(u["completion_tokens"]) if u.get("completion_tokens") is not None else max_tokens
        self.res.calls += 1
        self.res.prompt_tokens += pt
        self.res.completion_tokens += ct
        self.res.spent += self.prices.cost(model, pt, ct)
        return reply

    async def aclose(self) -> None:
        await self.llm.aclose()


@dataclass
class RateLimiter:
    """Per-visitor run limits plus a cap on concurrent runs. In memory: the
    durable limits are the spend cap and the day's run count in the ledger."""

    per_hour: int = 4
    per_day: int = 12
    concurrent: int = 2
    _seen: dict[str, deque] = field(default_factory=lambda: defaultdict(deque))
    _running: int = 0

    @classmethod
    def from_env(cls) -> "RateLimiter":
        return cls(per_hour=int(_env_float("FOOTFALL_VISITOR_RUNS_PER_HOUR", 4)),
                   per_day=int(_env_float("FOOTFALL_VISITOR_RUNS_PER_DAY", 12)),
                   concurrent=int(_env_float("FOOTFALL_CONCURRENT_RUNS", 2)))

    def check(self, visitor: str, now: float | None = None) -> str | None:
        now = time.time() if now is None else now
        if len(self._seen) > 5000:  # forget visitors with nothing in the last day
            for k in [k for k, q in self._seen.items() if not q or now - q[-1] > 86400]:
                del self._seen[k]
        q = self._seen[visitor]
        while q and now - q[0] > 86400:
            q.popleft()
        if len(q) >= self.per_day:
            return "You've had today's demo runs. Please come back tomorrow."
        if sum(1 for t in q if now - t <= 3600) >= self.per_hour:
            return "That's a few runs in an hour. Please wait a little before the next one."
        if self._running >= self.concurrent:
            return "The agent is busy with other visitors. Try again in a minute."
        return None

    def start(self, visitor: str, now: float | None = None) -> None:
        self._seen[visitor].append(time.time() if now is None else now)
        self._running += 1

    def finish(self) -> None:
        self._running = max(0, self._running - 1)
