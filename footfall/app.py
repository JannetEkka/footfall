"""Web app: a form, the agent's steps streamed live, and the checked plan.

Every run passes the visitor limits and reserves its spending ceiling in the
durable spend ledger before the first LLM call (budget.py), and settles the
actual cost afterwards, however it ends.

Demo shops (replay.py) run from their saved Qloo answers first and save new
ones while the Qloo key works. Once it doesn't (a 401/403, or
FOOTFALL_QLOO_OFFLINE=1), only the demo shops run, from their saved answers.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import llm as llm_mod
from . import store as store_mod
from .agent import KINDS, Agent, Shop
from .budget import FALLBACK_PRICES, Budget, BudgetError, MeteredLLM, Prices, RateLimiter, fetch_prices
from .geocode import GeocodeError
from .llm import LLMError
from .qloo import QlooClient, QlooError
from .replay import PRESETS, load_pack, save_recorded
from .store import StoreError

STATIC = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(title="Footfall")
app.mount("/static", StaticFiles(directory=STATIC), name="static")

limiter = RateLimiter.from_env()
_state: dict = {}


def budget() -> Budget:
    if "budget" not in _state:
        _state["budget"] = Budget.from_env(store_mod.from_env())
    return _state["budget"]


def qloo_offline() -> bool:
    return bool(os.environ.get("FOOTFALL_QLOO_OFFLINE")) or _state.get("qloo_dead", False)


async def pack(preset_id: str) -> dict:
    """The preset's saved Qloo answers, read from the store once per process."""
    packs = _state.setdefault("packs", {})
    if preset_id not in packs:
        packs[preset_id] = await load_pack(budget().store, preset_id)
    return packs[preset_id]


async def prices() -> Prices:
    """Token Factory's own prices, read once per process; the dated table if that fails."""
    if "prices" not in _state:
        live = {}
        if os.environ.get("LLM_PROVIDER", "nebius").lower() == "nebius":
            live = await fetch_prices(llm_mod.NEBIUS_BASE, os.environ.get("NEBIUS_HACK_API_KEY", ""))
        _state["prices"] = Prices({**FALLBACK_PRICES, **live})
    return _state["prices"]


def visitor(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() or (request.client.host if request.client else "unknown")


class PlanRequest(BaseModel):
    preset: str | None = None  # a demo shop; its own fields are used and the rest is ignored
    name: str = Field(default="", max_length=120)
    address: str = Field(default="", max_length=300)
    kind: str = "cafe"
    sells: str = Field(default="", max_length=600)
    lat: float | None = None
    lon: float | None = None
    radius_m: int = Field(default=1200, ge=200, le=5000)


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
async def health():
    llm = llm_mod.from_env()
    return {"qloo_key": bool(os.environ.get("QLOO_HACK_API_KEY")), "qloo": "replay" if qloo_offline() else "live",
            "llm": llm.label, "llm_key": llm.configured, "budget": await budget().status(),
            "presets": [{"id": k, "title": v["title"]} for k, v in PRESETS.items()],
            "kinds": {k: v["label"] for k, v in KINDS.items()}}


@app.post("/api/plan")
async def plan(req: PlanRequest, request: Request):
    offline = qloo_offline()
    saved = None
    if req.preset:
        if req.preset not in PRESETS:
            return JSONResponse({"error": "Unknown demo shop."}, status_code=404)
        shop_fields = PRESETS[req.preset]["shop"]
        try:
            saved = await pack(req.preset)
        except StoreError:
            if offline:
                return JSONResponse({"error": "The saved Qloo answers can't be read right now. "
                                              "Please try again in a minute."}, status_code=503)
            saved = {"bodies": {}, "recorded": None}
        qloo = QlooClient(pack=saved["bodies"], offline=offline)
    else:
        if offline:
            return JSONResponse({"error": "Qloo's hackathon API key has expired, so only the demo shops work "
                                          "now: they replay Qloo answers saved while the key worked."},
                                status_code=503)
        if not req.name.strip() or len(req.address.strip()) < 3:
            return JSONResponse({"error": "Please give the shop's name and street address."}, status_code=422)
        shop_fields = req.model_dump(exclude={"preset"})
        qloo = QlooClient()
    base_llm = llm_mod.from_env()
    if not qloo.configured or not base_llm.configured:
        missing = [n for n, ok in (("QLOO_HACK_API_KEY", qloo.configured), ("the LLM key", base_llm.configured))
                   if not ok]
        await qloo.aclose()
        await base_llm.aclose()
        return JSONResponse({"error": f"Server is missing {' and '.join(missing)}."}, status_code=503)

    who = visitor(request)
    refused = limiter.check(who)
    if refused:
        await qloo.aclose()
        await base_llm.aclose()
        return JSONResponse({"error": refused}, status_code=429)
    try:
        table = await prices()
        table.cost(base_llm.model, 0, 0)  # refuse a model with no known price before reserving
        res = await budget().reserve()
    except BudgetError as e:
        await qloo.aclose()
        await base_llm.aclose()
        return JSONResponse({"error": f"Sorry, {e}."}, status_code=503)
    limiter.start(who)

    llm = MeteredLLM(base_llm, res, table)
    agent = Agent(qloo, llm, temperature=0.0 if req.preset else 0.3,
                  saved_calls=saved.get("agent_calls", []) if saved and offline else None)
    shop = Shop(**shop_fields)

    async def stream():
        try:
            if req.preset:
                mode = {"type": "mode", "preset": req.preset, "shop": shop_fields, "replay": offline,
                        "saved": len(saved["bodies"]), "recorded": saved["recorded"]}
                yield f"data: {json.dumps(mode)}\n\n"
            async for ev in agent.run(shop):
                if ev["type"] == "plan":
                    ev["stats"].update(llm_cost_usd=round(res.spent, 5), qloo_live=qloo.requests_sent,
                                       qloo_saved=qloo.pack_hits)
                yield f"data: {json.dumps(ev)}\n\n"
        except (GeocodeError, QlooError, LLMError, ValueError) as e:
            yield f"data: {json.dumps({'type': 'error', 'text': str(e)})}\n\n"
        finally:
            limiter.finish()
            # Shielded so a closed browser tab can't skip the settlement.
            await asyncio.shield(_close(res, qloo, llm, agent, req.preset))
        yield "data: {\"type\": \"end\"}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


async def _close(res, qloo, llm, agent, preset_id) -> None:
    try:
        await budget().settle(res)
    except BudgetError:
        pass  # the full reservation stays counted, which errs on the safe side
    if qloo.auth_failed:
        _state["qloo_dead"] = True  # the key has been switched off: demo shops only from now on
    if preset_id and not qloo.offline and (qloo.recorded or agent.calls_made):
        try:
            new = await save_recorded(budget().store, preset_id, qloo.recorded, agent.calls_made)
            if new:
                _state.setdefault("packs", {})[preset_id] = new
        except StoreError:
            pass  # they'll be fetched again next time
    await qloo.aclose()
    await llm.aclose()
