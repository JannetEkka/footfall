import base64
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from footfall import app as app_mod
from footfall.budget import Budget, BudgetError, MeteredLLM, Prices, RateLimiter, Reservation, fetch_prices
from footfall.llm import Reply, ScriptedLLM
from footfall.store import Conflict, DirStore, GitHubStore

from test_footfall import FakeQloo, client

PRICES = Prices({"scripted": (1e-6, 2e-6)})


def budget(tmp_path, **kw):
    args = {"cap_usd": 0.1, "run_ceiling_usd": 0.04, "runs_per_day": 100} | kw
    return Budget(DirStore(tmp_path), **args)


# --- the spend ledger -----------------------------------------------------

@pytest.mark.asyncio
async def test_reservations_never_pass_the_cap(tmp_path):
    b = budget(tmp_path)
    await b.reserve()
    await b.reserve()
    with pytest.raises(BudgetError, match="used up"):
        await b.reserve()  # 0.04 + 0.04 + 0.04 > 0.1


@pytest.mark.asyncio
async def test_settling_replaces_the_reservation_with_the_actual_cost(tmp_path):
    b = budget(tmp_path)
    r = await b.reserve()
    r.spent = 0.003
    await b.settle(r)
    await b.settle(r)  # settling twice changes nothing
    status = await b.status()
    assert status["spent_usd"] == pytest.approx(0.003) and status["runs"] == 1


@pytest.mark.asyncio
async def test_an_unsettled_run_stays_counted_in_full(tmp_path):
    await budget(tmp_path).reserve()  # the server "dies" here
    assert (await budget(tmp_path).status())["spent_usd"] == pytest.approx(0.04)


@pytest.mark.asyncio
async def test_daily_run_limit_is_in_the_ledger(tmp_path):
    b = budget(tmp_path, runs_per_day=2, cap_usd=10)
    await b.reserve()
    await b.reserve()
    with pytest.raises(BudgetError, match="today"):
        await budget(tmp_path, runs_per_day=2, cap_usd=10).reserve()  # a "restarted" server knows too


@pytest.mark.asyncio
async def test_a_ledger_that_does_not_survive_restarts_means_no_llm_on_render(tmp_path, monkeypatch):
    monkeypatch.setenv("RENDER", "true")
    with pytest.raises(BudgetError, match="durable"):
        await Budget.from_env(DirStore(tmp_path)).reserve()


# --- every call is checked and priced -------------------------------------

@pytest.mark.asyncio
async def test_metered_llm_refuses_a_call_that_might_not_fit():
    res = Reservation(amount=0.001)
    llm = ScriptedLLM([Reply("ok", usage={"prompt_tokens": 100, "completion_tokens": 50})])
    m = MeteredLLM(llm, res, PRICES)
    with pytest.raises(BudgetError, match="ceiling"):
        await m.chat([{"role": "user", "content": "hi"}], max_tokens=1000)  # 1000 * 2e-6 * 1.25 > 0.001
    assert not llm.seen  # never sent


@pytest.mark.asyncio
async def test_metered_llm_prices_calls_from_reported_usage():
    res = Reservation(amount=1.0)
    m = MeteredLLM(ScriptedLLM([Reply("ok", usage={"prompt_tokens": 1000, "completion_tokens": 500})]), res, PRICES)
    await m.chat([{"role": "user", "content": "hi"}])
    assert res.spent == pytest.approx((1000 * 1e-6 + 500 * 2e-6) * 1.25)
    assert (res.calls, res.prompt_tokens, res.completion_tokens) == (1, 1000, 500)


@pytest.mark.asyncio
async def test_each_call_is_priced_at_its_own_models_rate():
    res = Reservation(amount=1.0)
    llm = ScriptedLLM([Reply("a", usage={"prompt_tokens": 1000, "completion_tokens": 0}),
                       Reply("b", usage={"prompt_tokens": 1000, "completion_tokens": 0})])
    m = MeteredLLM(llm, res, Prices({"scripted": (1e-6, 1e-6), "big": (1e-5, 1e-5)}))
    await m.chat([{"role": "user", "content": "x"}])
    await m.chat([{"role": "user", "content": "x"}], model="big")
    assert res.spent == pytest.approx((1000 * 1e-6 + 1000 * 1e-5) * 1.25)


def test_a_model_without_a_price_is_refused():
    with pytest.raises(BudgetError, match="no known price"):
        PRICES.cost("some/unpriced-model", 1, 1)


@pytest.mark.asyncio
async def test_prices_are_read_from_token_factory():
    def models(request):
        assert request.url.params["verbose"] == "true"
        return httpx.Response(200, json={"data": [{"id": "nvidia/x", "pricing": {"prompt": "0.0000001",
                                                                                  "completion": "0.0000004"}}]})

    assert await fetch_prices("https://tf.test/v1", "k", transport=httpx.MockTransport(models)) == {
        "nvidia/x": (1e-7, 4e-7)}
    assert await fetch_prices("https://tf.test/v1", "k",
                              transport=httpx.MockTransport(lambda r: httpx.Response(500))) == {}


# --- the durable store ----------------------------------------------------

class FakeGitHub:
    def __init__(self):
        self.files = {}
        self.puts = 0

    def __call__(self, request):
        path = request.url.path.split("/contents/", 1)[1]
        if request.method == "GET":
            if path not in self.files:
                return httpx.Response(404)
            text, sha = self.files[path]
            return httpx.Response(200, json={"encoding": "base64", "sha": sha,
                                             "content": base64.b64encode(text.encode()).decode()})
        body = json.loads(request.content)
        current = self.files.get(path, (None, None))[1]
        if body.get("sha") != current:
            return httpx.Response(409 if current else 422)
        self.puts += 1
        sha = f"sha{self.puts}"
        self.files[path] = (base64.b64decode(body["content"]).decode(), sha)
        return httpx.Response(200, json={"content": {"sha": sha}})


@pytest.mark.asyncio
async def test_github_store_round_trip_and_conflicts():
    gh = FakeGitHub()
    s = GitHubStore("owner/private", "token", transport=httpx.MockTransport(gh))
    assert await s.read("budget/spend.json") == (None, None)
    v1 = await s.write("budget/spend.json", {"a": 1}, None)
    assert await s.read("budget/spend.json") == ({"a": 1}, v1)
    with pytest.raises(Conflict):
        await s.write("budget/spend.json", {"a": 2}, None)  # someone else created it
    with pytest.raises(Conflict):
        await s.write("budget/spend.json", {"a": 2}, "stale-sha")


@pytest.mark.asyncio
async def test_budget_on_github_store():
    gh = FakeGitHub()
    b = Budget(GitHubStore("owner/private", "token", transport=httpx.MockTransport(gh)),
               cap_usd=1, run_ceiling_usd=0.05, runs_per_day=10, require_durable=True)
    r = await b.reserve()
    r.spent = 0.01
    await b.settle(r)
    assert (await b.status())["spent_usd"] == pytest.approx(0.01) and gh.puts == 2


@pytest.mark.asyncio
async def test_health_reads_of_the_ledger_are_cached():
    gh = FakeGitHub()
    reads = []
    transport = httpx.MockTransport(lambda r: (reads.append(r.method), gh(r))[1])
    b = Budget(GitHubStore("owner/private", "token", transport=transport), cap_usd=1, run_ceiling_usd=0.05,
               runs_per_day=10)
    for _ in range(5):
        await b.status()
    assert reads.count("GET") == 1
    await b.reserve()  # a run refreshes it
    assert (await b.status())["runs"] == 1


@pytest.mark.asyncio
async def test_unreachable_ledger_means_no_run():
    s = GitHubStore("owner/private", "token", transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    with pytest.raises(BudgetError, match="unavailable"):
        await Budget(s, cap_usd=1, run_ceiling_usd=0.05, runs_per_day=10).reserve()


# --- visitor limits -------------------------------------------------------

def test_visitor_limits():
    rl = RateLimiter(per_hour=2, per_day=3, concurrent=5)
    for t in (0, 10):
        assert rl.check("a", now=t) is None
        rl.start("a", now=t)
        rl.finish()
    assert "wait" in rl.check("a", now=20)
    assert rl.check("b", now=20) is None  # another visitor is fine
    assert rl.check("a", now=3700) is None
    rl.start("a", now=3700)
    rl.finish()
    assert "tomorrow" in rl.check("a", now=7400)
    assert rl.check("a", now=86400 + 20) is None


def test_concurrent_runs_are_capped():
    rl = RateLimiter(per_hour=10, per_day=10, concurrent=1)
    rl.start("a")
    assert "busy" in rl.check("b")
    rl.finish()
    assert rl.check("b") is None


# --- the app wires it all together ----------------------------------------

@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.setenv("QLOO_HACK_API_KEY", "test")
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.setattr(app_mod, "QlooClient", lambda **kw: client(FakeQloo()))
    draft = json.dumps({"summary": "s", "suggestions": []})
    monkeypatch.setattr(app_mod.llm_mod, "from_env",
                        lambda: ScriptedLLM(lambda msgs, tools: Reply("DONE" if tools else draft,
                                                                      usage={"prompt_tokens": 2000,
                                                                             "completion_tokens": 300})))
    monkeypatch.setattr(app_mod, "limiter", RateLimiter(per_hour=2, per_day=5, concurrent=2))
    monkeypatch.setattr(app_mod, "_state", {"prices": PRICES,
                                            "budget": Budget(DirStore(tmp_path), cap_usd=0.05,
                                                             run_ceiling_usd=0.02, runs_per_day=10)})
    return TestClient(app_mod.app)


SHOP_REQ = {"name": "Paper Lantern Cafe", "address": "6 Example Road", "kind": "cafe", "lat": 12.9716, "lon": 77.6412}


def _events(r):
    return [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ")]


def test_app_settles_the_actual_cost_after_a_run(web):
    evs = _events(web.post("/api/plan", json=SHOP_REQ))
    plan = next(e for e in evs if e["type"] == "plan")
    cost = (2 * 2000 * 1e-6 + 2 * 300 * 2e-6) * 1.25  # two LLM calls: investigate, write
    assert plan["stats"]["llm_cost_usd"] == pytest.approx(cost)
    status = web.get("/api/health").json()["budget"]
    assert status["spent_usd"] == pytest.approx(cost, abs=1e-4) and status["runs"] == 1


def test_app_rate_limits_a_visitor(web):
    for _ in range(2):
        assert web.post("/api/plan", json=SHOP_REQ).status_code == 200
    r = web.post("/api/plan", json=SHOP_REQ)
    assert r.status_code == 429 and "wait" in r.json()["error"]
    r = web.post("/api/plan", json=SHOP_REQ, headers={"X-Forwarded-For": "203.0.113.9"})
    assert r.status_code == 200


def test_app_refuses_when_the_budget_is_used_up(web):
    app_mod._state["budget"].cap = 0.01  # less than one run's ceiling
    r = web.post("/api/plan", json=SHOP_REQ)
    assert r.status_code == 503 and "budget" in r.json()["error"]
