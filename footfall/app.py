"""Web app: a form, the agent's steps streamed live, and the checked plan.

Every run passes the visitor limits and reserves its spending ceiling in the
durable spend ledger before the first LLM call (budget.py), and settles the
actual cost afterwards, however the run ends.
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

STATIC = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(title="Footfall")
app.mount("/static", StaticFiles(directory=STATIC), name="static")

limiter = RateLimiter.from_env()
_state: dict = {}


def budget() -> Budget:
    if "budget" not in _state:
        _state["budget"] = Budget.from_env(store_mod.from_env())
    return _state["budget"]


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
    name: str = Field(min_length=1, max_length=120)
    address: str = Field(min_length=3, max_length=300)
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
    return {"qloo_key": bool(os.environ.get("QLOO_HACK_API_KEY")), "llm": llm.label, "llm_key": llm.configured,
            "budget": await budget().status(), "kinds": {k: v["label"] for k, v in KINDS.items()}}


@app.post("/api/plan")
async def plan(req: PlanRequest, request: Request):
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
    agent = Agent(qloo, llm)
    shop = Shop(**req.model_dump())

    async def stream():
        try:
            async for ev in agent.run(shop):
                if ev["type"] == "plan":
                    ev["stats"]["llm_cost_usd"] = round(res.spent, 5)
                yield f"data: {json.dumps(ev)}\n\n"
        except (GeocodeError, QlooError, LLMError, ValueError) as e:
            yield f"data: {json.dumps({'type': 'error', 'text': str(e)})}\n\n"
        finally:
            limiter.finish()
            # Shielded so a closed browser tab can't skip the settlement.
            await asyncio.shield(_close(res, qloo, llm))
        yield "data: {\"type\": \"end\"}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


async def _close(res, qloo, llm) -> None:
    try:
        await budget().settle(res)
    except BudgetError:
        pass  # the full reservation stays counted, which errs on the safe side
    await qloo.aclose()
    await llm.aclose()
