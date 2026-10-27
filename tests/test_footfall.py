import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from footfall import checker, geocode as geocode_mod, params
from footfall.agent import Agent, Shop, address_overlap, is_taste_place, same_place
from footfall.evidence import Ledger
from footfall.geocode import GeocodeError
from footfall.llm import ChatLLM, Reply, ScriptedLLM, extract_json
from footfall.qloo import QlooClient, Tools, parse_entities, parse_heatmap, parse_tags, slim

FIX = Path(__file__).parent / "fixtures"
SHOP = (12.9716, 77.6412)


def fx(name):
    return json.loads((FIX / f"{name}.json").read_text())


class FakeQloo:
    """Routes requests to the synthetic fixtures and records what was sent."""

    def __init__(self, search=None, warn_unknown=None):
        self.requests = []
        self.search = search
        self.warn_unknown = warn_unknown  # a place ID that Qloo "doesn't recognise" as a signal

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        q = {k: v[0] for k, v in parse_qs(urlparse(str(request.url)).query).items()}
        path = request.url.path
        if path == "/search":
            return httpx.Response(200, json=self.search or fx("search_shop"))
        if path == "/v2/tags":
            return httpx.Response(200, json=fx("tag_search"))
        ft = q.get("filter.type")
        if ft == "urn:entity:place":
            return httpx.Response(200, json=fx("places"))
        if ft == "urn:entity:artist":
            body = fx("artists")
            if self.warn_unknown and self.warn_unknown in q.get("signal.interests.entities", ""):
                body["warnings"] = [{"type": "not_found", "signal.interests.entities": [self.warn_unknown]}]
            return httpx.Response(200, json=body)
        if ft == "urn:entity:book":
            return httpx.Response(200, json=fx("books"))
        if ft == "urn:entity:brand":
            return httpx.Response(200, json=fx("brands"))
        if ft == "urn:heatmap":
            return httpx.Response(200, json=fx("heatmap"))
        if ft == "urn:tag":
            ids = q.get("signal.interests.entities", "")
            if ids.startswith("PLACE-009"):  # test-only places Qloo has no tags for
                return httpx.Response(200, json={"success": True, "results": {"tags": []}})
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


def test_signal_location_is_allowed_where_verified_and_radius_stays_probe_only():
    for t in ("urn:entity:artist", "urn:entity:book", "urn:entity:brand", "urn:tag"):
        params.check({"filter.type": t, "signal.location": "POINT(1 2)"})
    p = {"filter.type": "urn:entity:artist", "signal.location": "POINT(1 2)", "signal.location.radius": 1000}
    with pytest.raises(params.UnsupportedParameter, match="signal.location.radius"):
        params.check(p)
    params.check(p, probe=True)
    with pytest.raises(params.UnsupportedParameter):  # movies weren't probed
        params.check({"filter.type": "urn:entity:movie", "signal.location": "POINT(1 2)"})


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


def test_slim_keeps_everything_the_parsers_read():
    for name in ("places", "artists", "search_shop"):
        assert parse_entities(slim(fx(name)), origin=SHOP) == parse_entities(fx(name), origin=SHOP)
    assert parse_tags(slim(fx("block_tags"))) == parse_tags(fx("block_tags"))
    assert parse_heatmap(slim(fx("heatmap"))) == parse_heatmap(fx("heatmap"))


def test_parse_tags_strips_names_and_reads_top_level_popularity():
    body = {"results": {"tags": [{"tag_id": "urn:tag:genre:music:indie", "name": " indie", "popularity": 0.8,
                                  "query": {"affinity": 0.9}}]}}
    assert parse_tags(body)[0]["name"] == "indie" and parse_tags(body)[0]["popularity"] == 0.8


@pytest.mark.asyncio
async def test_not_found_warning_is_recorded():
    fake = FakeQloo(warn_unknown="PLACE-0001")
    t = Tools(client(fake), Ledger(), origin=SHOP)
    r = await t.recommend("urn:entity:artist", entities=["PLACE-0001", "PLACE-0002"])
    assert r["not_found"] == ["PLACE-0001"] and t.ledger.calls[0].not_found == ["PLACE-0001"]


# --- locating the shop ----------------------------------------------------

def test_same_place_needs_the_shops_distinctive_words():
    assert same_place("Lantern Espresso", "Lantern Espresso Roastery & Cafe")
    assert same_place("Moth Art Cafe", "Moth Art Cafe")
    assert same_place("Café Lantern", "Cafe Lantern")
    assert not same_place("Moth Art Cafe", "Art Cafè")
    assert not same_place("Moth Art Cafe", "Café Coffee Corner")


def test_address_overlap():
    assert address_overlap("12 Example Street, London E9 9ZZ", "12 Example St London E9 9ZZ United Kingdom") >= 4
    assert address_overlap("12 Example Street, London E9 9ZZ", "40 Sample St Fakeville VIC 3999 Australia") == 0


def test_hotels_and_city_halls_are_not_taste_places():
    assert not is_taste_place({"genres": ["Hotel", "Place"]})
    assert not is_taste_place({"genres": ["Place", "City hall"]})
    assert not is_taste_place({"genres": ["Non denominational church", "Place"]})
    assert is_taste_place({"genres": ["Pasta shop", "Hotel"]})  # a café Qloo also tags as a hotel
    assert is_taste_place({})


@pytest.mark.asyncio
async def test_geocode_retries_once_on_429(monkeypatch):
    monkeypatch.setattr(geocode_mod, "RETRY_WAIT", 0)
    monkeypatch.setattr(geocode_mod, "_last", 0.0)
    calls = []

    def nominatim(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429)
        return httpx.Response(200, json=[{"lat": "1.5", "lon": "2.5", "display_name": "Somewhere"}])

    g = await geocode_mod.geocode("1 Retry Lane, Testville", transport=httpx.MockTransport(nominatim))
    assert len(calls) == 2 and g["lat"] == 1.5


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


def test_checker_cuts_items_of_the_wrong_kind_for_the_area():
    plan = {"suggestions": [{"area": "playlist", "title": "x", "items": [
        {"id": "ARTIST-0001", "name": "The Fixture Quartet"}, {"id": "urn:tag:keyword:jazz", "name": "Jazz"}],
        "evidence": []}, {"area": "stock", "title": "y", "items": [{"id": "urn:tag:keyword:jazz", "name": "Jazz"}],
                          "evidence": [{"id": "urn:tag:keyword:jazz"}]}]}
    out = checker.check(plan, _ledger())
    assert [i["name"] for i in out["suggestions"][0]["items"]] == ["The Fixture Quartet"]
    assert "can't be a urn:tag" in out["suggestions"][0]["checker_notes"][0]
    assert out["dropped"][0]["title"] == "y"  # a stock idea whose only item was a tag


def test_checker_takes_a_bare_geohash_as_its_heatmap_cell():
    led = _ledger()
    led.record("/v2/insights", {}, "heatmap", [{"id": "geohash:tdr1yx", "name": "tdr1yx", "kind": "heatmap"}])
    plan = {"suggestions": [{"area": "event", "title": "Listening night",
                             "items": [{"id": "tdr1yx", "name": "geohash:tdr1yx"}], "evidence": ["tdr1yx"]}]}
    out = checker.check(plan, led)
    assert out["suggestions"][0]["items"][0]["name"] == "tdr1yx" and not out["dropped"]


def test_checker_drops_suggestion_without_evidence():
    plan = {"suggestions": [{"area": "event", "title": "Open mic", "items": [], "evidence": [{"id": "made-up"}]}]}
    out = checker.check(plan, _ledger())
    assert out["suggestions"] == [] and "made-up" in out["dropped"][0]["reason"]


@pytest.mark.asyncio
async def test_thinking_is_switched_off_only_when_asked():
    bodies = []

    def tf(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
                                         "usage": {"prompt_tokens": 5, "completion_tokens": 1}})

    llm = ChatLLM("https://tf.test/v1", "k", "m", transport=httpx.MockTransport(tf),
                  no_think={"chat_template_kwargs": {"enable_thinking": False}})
    await llm.chat([{"role": "user", "content": "a"}])
    r = await llm.chat([{"role": "user", "content": "b"}], think=False)
    assert "chat_template_kwargs" not in bodies[0]
    assert bodies[1]["chat_template_kwargs"] == {"enable_thinking": False} and "response_format" not in bodies[1]
    assert r.finish_reason == "stop" and llm.tokens == {"prompt": 10, "completion": 2}


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
    assert kinds[-1] == "plan" and "taste" in kinds

    taste = next(e for e in events if e["type"] == "taste")
    assert taste["on_qloo"] and [d["label"] for d in taste["domains"]] == ["Music", "Food"]
    music = taste["domains"][0]
    assert {t["name"] for t in music["missing"]} >= {"Jazz", "Vinyl"}
    assert {t["name"] for t in music["shared"]} == {"Vegan", "Matcha"}

    plan = events[-1]["plan"]
    titles = [s["title"] for s in plan["suggestions"]]
    assert titles == ["Weekday playlist", "Saturday listening session"]
    assert [i["name"] for i in plan["suggestions"][0]["items"]] == ["The Fixture Quartet", "DJ Placeholder"]
    assert plan["dropped"][0]["title"] == "Hallucinated shelf"

    # Neither the shop itself nor the hotel is part of the neighbourhood signal.
    sent = [parse_qs(r.url.query.decode()) for r in fake.requests]
    artist_q = next(s for s in sent if s.get("filter.type") == ["urn:entity:artist"])
    assert "PLACE-0006" not in artist_q["signal.interests.entities"][0]
    assert "PLACE-0007" not in artist_q["signal.interests.entities"][0]
    # The wider-area baseline uses signal.location, without a radius.
    area_q = next(s for s in sent if "signal.location" in s)
    assert area_q["filter.type"] == ["urn:entity:artist"] and "signal.location.radius" not in area_q
    # Taste tags are asked for one domain at a time.
    assert {s.get("filter.tag.types", [""])[0] for s in sent if s.get("filter.type") == ["urn:tag"]} == {
        "urn:tag:genre:music", "urn:tag:specialty_dish:place"}
    # Every request went to the hackathon host.
    assert {r.url.host for r in fake.requests} == {"hackathon.api.qloo.com"}
    assert events[-1]["stats"]["agent_calls"] == 1


def _script():
    return ScriptedLLM([Reply("DONE"), Reply(json.dumps({"summary": "", "suggestions": []}))])


async def _geo(addr):
    return {"lat": SHOP[0], "lon": SHOP[1], "label": "6 Example Road"}


@pytest.mark.asyncio
async def test_nearest_place_with_another_name_is_not_the_shop():
    other = {"results": [{"name": "Café Coffee Day", "entity_id": "PLACE-0099", "location": {"lat": 12.9717,
                                                                                             "lon": 77.6413}}]}
    fake = FakeQloo(search=other)
    agent = Agent(client(fake), _script(), geocoder=_geo)
    events = [e async for e in agent.run(Shop(name="Paper Lantern Cafe", address="6 Example Road"))]
    taste = next(e for e in events if e["type"] == "taste")
    assert not taste["on_qloo"] and taste["domains"][0]["missing"] is None


@pytest.mark.asyncio
async def test_agent_recentres_on_qloos_location_for_the_shop():
    async def district(addr):  # a geocoder that only finds the district, 1 km off
        return {"lat": SHOP[0] + 0.009, "lon": SHOP[1], "label": "Example District"}

    fake = FakeQloo()
    agent = Agent(client(fake), _script(), geocoder=district)
    events = [e async for e in agent.run(Shop(name="Paper Lantern Cafe", address="6 Example Road"))]
    located = [e for e in events if e["type"] == "located"]
    assert len(located) == 2 and located[-1]["lat"] == 12.9717
    places_q = next(parse_qs(r.url.query.decode()) for r in fake.requests
                    if r.url.path == "/v2/insights" and "filter.location" in parse_qs(r.url.query.decode()))
    assert places_q["filter.location"] == ["POINT(77.6413 12.9717)"]


@pytest.mark.asyncio
async def test_geocoder_failure_falls_back_to_qloo_search():
    async def down(addr):
        raise GeocodeError("geocoder returned 429")

    fake = FakeQloo()
    agent = Agent(client(fake), _script(), geocoder=down)
    events = [e async for e in agent.run(Shop(name="Paper Lantern Cafe", address="6 Example Road, Bengaluru"))]
    assert events[-1]["type"] == "plan"
    located = next(e for e in events if e["type"] == "located")
    assert (located["lat"], located["lon"]) == (12.9717, 77.6413)

    fake = FakeQloo()
    agent = Agent(client(fake), _script(), geocoder=down)
    events = [e async for e in agent.run(Shop(name="Paper Lantern Cafe", address="9 Elsewhere Lane, Mumbai"))]
    assert events[-1]["type"] == "error" and "Couldn't place" in events[-1]["text"]


@pytest.mark.asyncio
async def test_unrecognised_signal_places_are_replaced():
    fake = FakeQloo(warn_unknown="PLACE-0001")
    agent = Agent(client(fake), _script(), geocoder=_geo)
    [e async for e in agent.run(Shop(name="Paper Lantern Cafe", address="6 Example Road"))]
    sent = [parse_qs(r.url.query.decode()) for r in fake.requests]
    tag_q = [s for s in sent if s.get("filter.type") == ["urn:tag"]]
    assert tag_q and all("PLACE-0001" not in s["signal.interests.entities"][0] for s in tag_q
                         if s["signal.interests.entities"][0] != "PLACE-0006")


@pytest.mark.asyncio
async def test_nearest_branch_wins_and_no_shop_tags_means_no_comparison():
    branches = {"results": [
        {"name": "Paper Lantern Cafe - Station Branch", "entity_id": "PLACE-0098",
         "location": {"lat": 12.9735, "lon": 77.6413}},
        {"name": "Paper Lantern Cafe", "entity_id": "PLACE-0097", "location": {"lat": 12.9717, "lon": 77.6413}},
    ]}
    fake = FakeQloo(search=branches)
    agent = Agent(client(fake), _script(), geocoder=_geo)
    events = [e async for e in agent.run(Shop(name="Paper Lantern Cafe", address="6 Example Road"))]
    own_q = [parse_qs(r.url.query.decode()) for r in fake.requests
             if parse_qs(r.url.query.decode()).get("signal.interests.entities") == ["PLACE-0097"]]
    assert own_q  # the nearer branch is the shop
    taste = next(e for e in events if e["type"] == "taste")
    assert taste["on_qloo"] and taste["domains"][0]["missing"] is None  # the fixture has no tags for PLACE-0097
