"""Web app: a form, the agent's steps streamed live, and the checked plan."""

from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import llm as llm_mod
from .agent import KINDS, Agent, Shop
from .geocode import GeocodeError
from .llm import LLMError
from .qloo import QlooClient, QlooError

STATIC = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(title="Footfall")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


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
            "kinds": {k: v["label"] for k, v in KINDS.items()}}


@app.post("/api/plan")
async def plan(req: PlanRequest):
    qloo = QlooClient()
    llm = llm_mod.from_env()
    if not qloo.configured or not llm.configured:
        missing = [n for n, ok in (("QLOO_HACK_API_KEY", qloo.configured), ("the LLM key", llm.configured)) if not ok]
        return JSONResponse({"error": f"Server is missing {' and '.join(missing)}."}, status_code=503)
    agent = Agent(qloo, llm)
    shop = Shop(**req.model_dump())

    async def stream():
        try:
            async for ev in agent.run(shop):
                yield f"data: {json.dumps(ev)}\n\n"
        except (GeocodeError, QlooError, LLMError, ValueError) as e:
            yield f"data: {json.dumps({'type': 'error', 'text': str(e)})}\n\n"
        finally:
            await qloo.aclose()
            await llm.aclose()
        yield "data: {\"type\": \"end\"}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
