import json

import httpx
import pytest
from fastapi.testclient import TestClient

from footfall import app as app_mod
from footfall.budget import Budget, Prices, RateLimiter
from footfall.llm import Reply, ScriptedLLM
from footfall.qloo import OFFLINE_MISS, QlooClient, QlooError, cache_key
from footfall.replay import PRESETS, load_pack, save_recorded
from footfall.store import DirStore

from test_footfall import FakeQloo

PLACES_Q = {"filter.type": "urn:entity:place", "filter.location": "POINT(1 2)", "filter.location.radius": 500,
            "filter.popularity.min": 0.3}


@pytest.mark.asyncio
async def test_offline_client_answers_from_the_pack_and_never_goes_live():
    fake = FakeQloo()
    key, _ = cache_key("/v2/insights", PLACES_Q)
    q = QlooClient("", transport=httpx.MockTransport(fake), cache_dir="", offline=True,
                   pack={key: {"results": {"entities": []}, "saved": True}})
    assert q.configured
    assert (await q.insights(PLACES_Q))["saved"] and q.pack_hits == 1
    with pytest.raises(QlooError, match="saved Qloo answers"):
        await q.insights({**PLACES_Q, "take": 3})
    assert not fake.requests and OFFLINE_MISS.startswith("not among")


@pytest.mark.asyncio
async def test_live_client_records_what_it_fetches_and_notices_a_dead_key():
    fake = FakeQloo()
    q = QlooClient("k", transport=httpx.MockTransport(fake), cache_dir="")
    await q.insights(PLACES_Q)
    assert list(q.recorded) == [cache_key("/v2/insights", PLACES_Q)[0]]

    dead = QlooClient("k", transport=httpx.MockTransport(lambda r: httpx.Response(401, json={"error": "x"})),
                      cache_dir="")
    with pytest.raises(QlooError):
        await dead.insights(PLACES_Q)
    assert dead.auth_failed


@pytest.mark.asyncio
async def test_packs_merge_and_stay_under_the_size_limit(tmp_path, monkeypatch):
    store = DirStore(tmp_path)
    pid = "shoreditch-cafe"
    assert (await load_pack(store, pid))["bodies"] == {}
    await save_recorded(store, pid, {"a": {"x": 1}})
    await save_recorded(store, pid, {"a": {"x": 2}, "b": {"y": 1}})  # "a" is kept as first saved
    assert (await load_pack(store, pid))["bodies"] == {"a": {"x": 1}, "b": {"y": 1}}
    assert await save_recorded(store, pid, {"a": {"x": 3}}) is None  # nothing new
    monkeypatch.setattr("footfall.replay.PACK_LIMIT_BYTES", 10)
    assert await save_recorded(store, pid, {"c": {"z": 1}}) is None
    with pytest.raises(KeyError):
        await load_pack(store, "../../etc/passwd")


@pytest.fixture
def web(tmp_path, monkeypatch):
    fake = FakeQloo()
    monkeypatch.setenv("QLOO_HACK_API_KEY", "test")
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.delenv("FOOTFALL_QLOO_OFFLINE", raising=False)
    monkeypatch.setattr(app_mod, "QlooClient",
                        lambda **kw: QlooClient("test", transport=httpx.MockTransport(fake), cache_dir="", **kw))
    llms = []

    def make_llm():
        draft = json.dumps({"summary": "s", "suggestions": []})
        llms.append(ScriptedLLM(lambda msgs, tools: Reply("DONE" if tools else draft,
                                                         usage={"prompt_tokens": 100, "completion_tokens": 10})))
        return llms[-1]

    monkeypatch.setattr(app_mod.llm_mod, "from_env", make_llm)
    monkeypatch.setattr(app_mod, "limiter", RateLimiter(per_hour=50, per_day=50, concurrent=2))
    monkeypatch.setattr(app_mod, "_state", {"prices": Prices({"scripted": (1e-7, 1e-7)}),
                                            "budget": Budget(DirStore(tmp_path), cap_usd=1, run_ceiling_usd=0.02,
                                                             runs_per_day=50)})
    return TestClient(app_mod.app), fake, llms, tmp_path


def _events(r):
    return [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ")]


def test_a_demo_shop_is_recorded_live_then_replayed_offline(web, monkeypatch):
    http, fake, llms, tmp_path = web
    evs = _events(http.post("/api/plan", json={"preset": "brooklyn-bookshop", "name": "ignored"}))
    assert evs[0]["type"] == "mode" and evs[0]["shop"]["name"] == PRESETS["brooklyn-bookshop"]["shop"]["name"]
    assert all(o.get("temperature") == 0.0 for o in llms[-1].options)  # same tool calls next time
    live_calls = len(fake.requests)
    assert live_calls > 5
    pack = json.loads((tmp_path / "replay" / "brooklyn-bookshop.json").read_text())
    assert len(pack["bodies"]) == live_calls

    monkeypatch.setenv("FOOTFALL_QLOO_OFFLINE", "1")
    app_mod._state.pop("packs", None)  # a fresh process reads the pack from the store
    evs = _events(http.post("/api/plan", json={"preset": "brooklyn-bookshop"}))
    assert evs[0]["replay"] and evs[0]["saved"] == live_calls
    plan = next(e for e in evs if e["type"] == "plan")
    assert plan["stats"]["qloo_live"] == 0 and plan["stats"]["qloo_saved"] == live_calls
    assert len(fake.requests) == live_calls  # not one more request to Qloo


def test_offline_refuses_custom_addresses_and_unknown_presets(web, monkeypatch):
    http, *_ = web
    monkeypatch.setenv("FOOTFALL_QLOO_OFFLINE", "1")
    r = http.post("/api/plan", json={"name": "Somewhere", "address": "1 Any Street"})
    assert r.status_code == 503 and "demo shops" in r.json()["error"]
    assert http.post("/api/plan", json={"preset": "nowhere"}).status_code == 404
    assert http.get("/api/health").json()["qloo"] == "replay"


def test_a_dead_key_switches_the_app_to_replay(web, monkeypatch):
    http, *_ = web
    monkeypatch.setattr(app_mod, "QlooClient", lambda **kw: QlooClient(
        "test", transport=httpx.MockTransport(lambda r: httpx.Response(403, json={})), cache_dir="", **kw))
    http.post("/api/plan", json={"name": "Paper Lantern Cafe", "address": "6 Example Road", "lat": 1, "lon": 2})
    assert http.get("/api/health").json()["qloo"] == "replay"


def test_custom_runs_need_a_name_and_address(web):
    http, *_ = web
    assert http.post("/api/plan", json={"name": "", "address": "x"}).status_code == 422


def test_replay_offers_the_saved_tool_calls_and_answers_them(web, monkeypatch):
    http, fake, llms, tmp_path = web
    heat = {"id": "c1", "name": "heatmap", "arguments": {"tags": ["urn:tag:keyword:vinyl"], "why": "event spot"}}
    draft = json.dumps({"summary": "s", "suggestions": []})

    def scripted():
        llms.append(ScriptedLLM([Reply("", tool_calls=[heat], usage={"prompt_tokens": 1, "completion_tokens": 1}),
                                 Reply("DONE"), Reply(draft)]))
        return llms[-1]

    monkeypatch.setattr(app_mod.llm_mod, "from_env", scripted)
    http.post("/api/plan", json={"preset": "koramangala-cafe"})  # live: records the heatmap call
    pack = json.loads((tmp_path / "replay" / "koramangala-cafe.json").read_text())
    assert pack["agent_calls"] == [{"name": "heatmap", "arguments": heat["arguments"]}]

    monkeypatch.setenv("FOOTFALL_QLOO_OFFLINE", "1")
    app_mod._state.pop("packs", None)
    before = len(fake.requests)
    evs = _events(http.post("/api/plan", json={"preset": "koramangala-cafe"}))
    ask = llms[-1].seen[0][1]["content"]
    assert "replay mode" in ask and '"saved_calls"' in ask and "urn:tag:keyword:vinyl" in ask
    agent_ev = next(e for e in evs if e["type"] == "qloo" and e.get("by") == "agent")
    assert not agent_ev["error"] and len(fake.requests) == before
