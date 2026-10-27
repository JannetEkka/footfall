import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from footfall import checker, params
from footfall.agent import Agent, Shop
from footfall.evidence import Ledger
from footfall.llm import Reply, ScriptedLLM, extract_json
from footfall.qloo import QlooClient, Tools, parse_entities

FIX = Path(__file__).parent / "fixtures"
SHOP = (12.9716, 77.6412)


def fx(name):
    return json.loads((FIX / f"{name}.json").read_text())


class FakeQloo:
    """Routes requests to the synthetic fixtures and records what was sent."""

    def __init__(self):
        self.requests = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        q = {k: v[0] for k, v in parse_qs(urlparse(str(request.url)).query).items()}
        path = request.url.path
        if path == "/search":
            return httpx.Response(200, json=fx("search_shop"))
        if path == "/v2/tags":
            return httpx.Response(200, json=fx("tag_search"))
        ft = q.get("filter.type")
        if ft == "urn:entity:place":
            return httpx.Response(200, json=fx("places"))
        if ft == "urn:entity:artist":
            return httpx.Response(200, json=fx("artists"))
        if ft == "urn:entity:book":
            return httpx.Response(200, json=fx("books"))
        if ft == "urn:entity:brand":
            return httpx.Response(200, json=fx("brands"))
        if ft == "urn:heatmap":
            return httpx.Response(200, json=fx("heatmap"))
        if ft == "urn:tag":
            ids = q.get("signal.interests.entities", "")
            return httpx.Response(200, json=fx("own_tags" if ids == "PLACE-0006" else "block_tags"))
        return httpx.Response(200, json={"success": True, "results": {"entities": []}})


def client(fake):
    return QlooClient("test-key", transport=httpx.MockTransport(fake), cache_dir="")


# --- params ---------------------------------------------------------------

def test_location_filter_is_only_documented_for_geo_types():
    params.check({"filter.type": "urn:entity:place", "filter.location": "POINT(1 2)", "filter.location.radius": 500})
    params.check({"filter.type": "urn:heatmap", "filter.location": "POINT(1 2)"})
    with pytest.raises(params.UnsupportedParameter, match="filter.location"):
        params.check({"filter.type": "urn:entity:artist", "filter.location": "POINT(1 2)"})


def test_signal_location_on_artists_needs_probe_flag():
    p = {"filter.type": "urn:entity:artist", "signal.location": "POINT(1 2)"}
    with pytest.raises(params.UnsupportedParameter):
        params.check(p)
    params.check(p, probe=True)


def test_typos_are_refused_not_silently_ignored():
    with pytest.raises(params.UnsupportedParameter, match="filter.tag "):
        params.check({"filter.type": "urn:entity:place", "filter.tag": "x"})


# --- client ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_client_uses_hackathon_host_header_and_get():
    fake = FakeQloo()
    q = client(fake)
    await q.insights({"filter.type": "urn:entity:place", "filter.location": "POINT(77.6 12.9)",
                      "filter.location.radius": 800, "filter.tags": ["a", "b"]})
    req = fake.requests[0]
    assert req.method == "GET"
    assert req.url.host == "hackathon.api.qloo.com"
    assert req.headers["X-Api-Key"] == "test-key"
    assert "authorization" not in req.headers
    qs = parse_qs(req.url.query.decode())
    assert qs["filter.tags"] == ["a,b"]
    assert qs["filter.location.radius"] == ["800"]


@pytest.mark.asyncio
async def test_client_caches_identical_requests():
    fake = FakeQloo()
    q = client(fake)
    p = {"filter.type": "urn:entity:artist", "signal.interests.entities": ["X"]}
    await q.insights(p)
    await q.insights(dict(p))
    assert len(fake.requests) == 1


@pytest.mark.asyncio
async def test_no_key_means_no_request():
    fake = FakeQloo()
    q = QlooClient("", transport=httpx.MockTransport(fake), cache_dir="")
    t = Tools(q, Ledger(), origin=SHOP)
    r = await t.recommend("urn:entity:artist", entities=["X"])
    assert "QLOO_HACK_API_KEY" in r["error"] and not fake.requests


def test_parse_entities_adds_distance():
    items = parse_entities(fx("places"), origin=SHOP)
    shop = next(i for i in items if i["id"] == "PLACE-0006")
    assert shop["extra"]["distance_m"] < 30
    assert items[0]["extra"]["tags"]


@pytest.mark.asyncio
async def test_places_near_sends_wkt_lon_lat():
    fake = FakeQloo()
    t = Tools(client(fake), Ledger(), origin=SHOP)
    await t.places_near(radius_m=900)
    qs = parse_qs(fake.requests[0].url.query.decode())
    assert qs["filter.location"] == [f"POINT({SHOP[1]} {SHOP[0]})"]


# --- checker --------------------------------------------------------------

def _ledger():
    led = Ledger()
    led.record("/v2/insights", {}, "music", [
        {"id": "ARTIST-0001", "name": "The Fixture Quartet", "kind": "urn:entity:artist"},
        {"id": "ARTIST-0002", "name": "DJ Placeholder", "kind": "urn:entity:artist"},
    ])
    led.record("/v2/insights", {}, "tags", [{"id": "urn:tag:keyword:jazz", "name": "Jazz", "kind": "urn:tag"}])
    return led


def test_checker_cuts_items_qloo_never_returned():
    plan = {"suggestions": [{"area": "playlist", "title": "Jazz mornings",
                             "items": [{"id": "ARTIST-0001", "name": "The Fixture Quartet"},
                                       {"id": "ARTIST-9999", "name": "Miles Davis"}],
                             "evidence": [{"id": "urn:tag:keyword:jazz"}]}]}
    out = checker.check(plan, _ledger())
    s = out["suggestions"][0]
    assert [i["name"] for i in s["items"]] == ["The Fixture Quartet"]
    assert any("Miles Davis" in n for n in s["checker_notes"])


def test_checker_cuts_right_id_wrong_name():
    plan = {"suggestions": [{"area": "playlist", "title": "x",
                             "items": [{"id": "ARTIST-0001", "name": "Somebody Else"}], "evidence": []}]}
    out = checker.check(plan, _ledger())
    assert not out["suggestions"] and out["dropped"][0]["title"] == "x"


def test_checker_drops_suggestion_without_evidence():
    plan = {"suggestions": [{"area": "event", "title": "Open mic", "items": [], "evidence": [{"id": "made-up"}]}]}
    out = checker.check(plan, _ledger())
    assert out["suggestions"] == [] and "made-up" in out["dropped"][0]["reason"]


def test_extract_json_handles_think_and_fences():
    assert extract_json('<think>{"no": 1}</think>\n```json\n{"a": {"b": "}"}}\n```') == {"a": {"b": "}"}}


# --- agent end to end (synthetic Qloo, scripted LLM) ----------------------

@pytest.mark.asyncio
async def test_agent_end_to_end():
    fake = FakeQloo()
    draft = {
        "summary": "A late-night, vinyl-and-vegan block.",
        "suggestions": [
            {"area": "playlist", "title": "Weekday playlist", "what": "Play these.", "why": "Block's music.",
             "items": [{"id": "ARTIST-0001", "name": "The Fixture Quartet"},
                       {"id": "ARTIST-0002", "name": "DJ Placeholder"},
                       {"id": "ARTIST-4242", "name": "Invented Band"}],
             "evidence": [{"id": "PLACE-0002"}]},
            {"area": "event", "title": "Saturday listening session", "what": "x", "why": "y", "items": [],
             "evidence": [{"id": "geohash:tdr1yx"}, {"id": "urn:tag:keyword:vinyl"}]},
            {"area": "stock", "title": "Hallucinated shelf", "what": "x", "why": "y",
             "items": [{"id": "BOOK-7777", "name": "Not A Real Result"}], "evidence": []},
        ],
    }
    llm = ScriptedLLM([
        Reply("", tool_calls=[{"id": "c1", "name": "heatmap",
                               "arguments": {"tags": ["urn:tag:keyword:vinyl"], "why": "where vinyl fans are"}}]),
        Reply("DONE"),
        Reply(json.dumps(draft)),
    ])

    async def geo(addr):
        return {"lat": SHOP[0], "lon": SHOP[1], "label": "6 Example Road"}

    agent = Agent(client(fake), llm, geocoder=geo)
    events = [e async for e in agent.run(Shop(name="Paper Lantern Cafe", address="6 Example Road", kind="cafe"))]
    kinds = [e["type"] for e in events]
    assert kinds[-1] == "plan" and "gap" in kinds

    gap = next(e for e in events if e["type"] == "gap")
    assert {t["name"] for t in gap["block_not_shop"]} >= {"Jazz", "Vinyl"}
    assert {t["name"] for t in gap["shared"]} == {"Vegan", "Matcha"}

    plan = events[-1]["plan"]
    titles = [s["title"] for s in plan["suggestions"]]
    assert titles == ["Weekday playlist", "Saturday listening session"]
    assert [i["name"] for i in plan["suggestions"][0]["items"]] == ["The Fixture Quartet", "DJ Placeholder"]
    assert plan["dropped"][0]["title"] == "Hallucinated shelf"

    # The shop itself is not part of the neighbourhood signal.
    sent = [parse_qs(r.url.query.decode()) for r in fake.requests]
    artist_q = next(s for s in sent if s.get("filter.type") == ["urn:entity:artist"])
    assert "PLACE-0006" not in artist_q["signal.interests.entities"][0]
    # Every request went to the hackathon host.
    assert {r.url.host for r in fake.requests} == {"hackathon.api.qloo.com"}
    assert events[-1]["stats"]["agent_calls"] == 1
