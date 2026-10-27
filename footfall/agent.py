"""The Footfall agent.

1. Locate the shop (geocode, or Qloo's /search if the geocoder fails) and
   look for it on Qloo; if Qloo knows it, centre on Qloo's location for it.
2. Survey the block: the places within walking distance that locals go to.
   These place IDs become the "neighbourhood signal": Qloo filters only
   places by location, but every entity type accepts place IDs as
   signal.interests.entities, so the block's places can carry its taste
   over to music, books, brands and films. signal.location (the wider area,
   not the block) is the baseline the block's music is compared with. Tag
   taste per domain (music genres, dishes, book genres) gives the taste gap.
3. Investigate: the LLM (Nemotron on Token Factory by default) runs its own
   Qloo tool calls on top of the survey, up to MAX_TOOL_CALLS.
4. Write: the LLM drafts a playlist, stock or menu ideas and one weekend
   event, citing Qloo IDs.
5. Check: checker.py cuts anything Qloo didn't return.

run() yields events (dicts) so the UI can show the agent working.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, AsyncIterator

from . import checker
from .budget import BudgetError
from .evidence import Ledger
from .geocode import GeocodeError, geocode
from .llm import extract_json
from .qloo import NOT_TASTE, QlooClient, Tools

MAX_TOOL_CALLS = 8
WRITE_MAX_TOKENS = 3000

# Tag taste, one domain at a time. Unfiltered urn:tag results mix in credit
# cards and hotel ratings (first live run), so each domain filters by tag type.
# Music genre tags can come back empty for places (Bengaluru, 10-27) while the
# block's artists are plentiful; then the genres are read off those artists.
MUSIC = {"label": "Music", "tag_types": ["urn:tag:genre:music"], "via_artists": True}
FOOD = {"label": "Food", "tag_types": ["urn:tag:specialty_dish:place"]}
BOOKS = {"label": "Books", "parents_types": ["urn:entity:book"]}

# What each kind of shop can change, and which Qloo types feed it.
KINDS: dict[str, dict[str, Any]] = {
    "cafe": {"label": "café", "areas": ["playlist", "menu", "stock", "event"],
             "stock_types": ["urn:entity:book", "urn:entity:brand"], "taste": [MUSIC, FOOD]},
    "bookshop": {"label": "bookshop", "areas": ["stock", "playlist", "event"],
                 "stock_types": ["urn:entity:book", "urn:entity:person"], "taste": [BOOKS, MUSIC]},
    "record_shop": {"label": "record shop", "areas": ["stock", "playlist", "event"],
                    "stock_types": ["urn:entity:artist"], "taste": [MUSIC]},
    "bar": {"label": "bar", "areas": ["playlist", "menu", "event"],
            "stock_types": ["urn:entity:brand"], "taste": [MUSIC, FOOD]},
    "other": {"label": "shop", "areas": ["playlist", "stock", "event"],
              "stock_types": ["urn:entity:brand", "urn:entity:book"], "taste": [MUSIC]},
}

SIGNAL_SIZE = 8

# Words that don't identify a shop: "Lantern Espresso" is Lantern.
_GENERIC = {"the", "a", "an", "and", "of", "at", "on", "cafe", "coffee", "espresso", "bar", "books", "book",
            "bookshop", "bookstore", "shop", "store", "records", "record", "restaurant", "kitchen", "bakery",
            "co", "company", "ltd", "house"}
_STREET = {"street", "road", "avenue", "lane", "st", "rd", "ave", "the", "and"}


def _words(s: str) -> list[str]:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).split()


def same_place(shop_name: str, qloo_name: str) -> bool:
    """Is this Qloo place the shop? All of the shop name's distinctive words
    must be in Qloo's name (or Qloo's two or more distinctive words in the shop's)."""
    a, b = _words(shop_name), _words(qloo_name)
    if not a or not b:
        return False
    if a == b:
        return True
    core_a, core_b = set(a) - _GENERIC, set(b) - _GENERIC
    return bool(core_a and core_a <= set(b)) or (len(core_b) >= 2 and core_b <= set(a))


def address_overlap(a: str, b: str) -> int:
    """Shared address words (postcodes, street and city names) between two addresses."""
    keep = lambda s: {w for w in _words(s) if (len(w) >= 3 or any(c.isdigit() for c in w)) and w not in _STREET}
    return len(keep(a) & keep(b))


def is_taste_place(p: dict) -> bool:
    """False for hotels, city halls, churches, clinics and the like: popular, but
    not where a street's taste shows. Judged on the first genre that isn't 'Place'."""
    genres = [g for g in p.get("genres") or [] if g.lower() != "place"]
    if not genres:
        return True
    g = genres[0].lower()
    return not any(re.search(rf"\b{re.escape(w)}\b", g) for w in NOT_TASTE)


@dataclass
class Shop:
    name: str
    address: str
    kind: str = "cafe"
    sells: str = ""
    lat: float | None = None
    lon: float | None = None
    radius_m: int = 1200


TOOL_SPECS = [
    {"type": "function", "function": {
        "name": "recommend",
        "description": "Qloo /v2/insights: entities of one type that people with the given tastes like. "
                       "Pass place or other entity IDs from earlier results as `entities`, and/or tag IDs as `tags`.",
        "parameters": {"type": "object", "properties": {
            "filter_type": {"type": "string", "enum": ["urn:entity:artist", "urn:entity:book", "urn:entity:brand",
                                                        "urn:entity:movie", "urn:entity:podcast", "urn:entity:person",
                                                        "urn:entity:tv_show", "urn:entity:video_game"]},
            "entities": {"type": "array", "items": {"type": "string"}},
            "tags": {"type": "array", "items": {"type": "string"}},
            "take": {"type": "integer", "minimum": 3, "maximum": 15},
            "why": {"type": "string", "description": "one line: what you want to learn"}},
            "required": ["filter_type", "why"]}}},
    {"type": "function", "function": {
        "name": "find_tags",
        "description": "Qloo /v2/tags: find tag IDs (genres, cuisines, interests) by keyword.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"},
            "parents_types": {"type": "array", "items": {"type": "string"}},
            "why": {"type": "string"}}, "required": ["query", "why"]}}},
    {"type": "function", "function": {
        "name": "places_near",
        "description": "Qloo /v2/insights for places within radius_m of the shop, optionally narrowed by tag IDs "
                       "or ranked by similarity to entity IDs.",
        "parameters": {"type": "object", "properties": {
            "radius_m": {"type": "integer", "minimum": 200, "maximum": 5000},
            "tags": {"type": "array", "items": {"type": "string"}},
            "entities": {"type": "array", "items": {"type": "string"}},
            "why": {"type": "string"}}, "required": ["why"]}}},
    {"type": "function", "function": {
        "name": "heatmap",
        "description": "Qloo heatmap: which spots around the shop have the strongest affinity for the given "
                       "entities or tags (use it to place an event or flyers).",
        "parameters": {"type": "object", "properties": {
            "entities": {"type": "array", "items": {"type": "string"}},
            "tags": {"type": "array", "items": {"type": "string"}},
            "why": {"type": "string"}}, "required": ["why"]}}},
    {"type": "function", "function": {
        "name": "find_entity",
        "description": "Qloo /search: look up an entity ID by name (an artist, book, brand or place).",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"},
            "types": {"type": "array", "items": {"type": "string"}},
            "why": {"type": "string"}}, "required": ["query", "why"]}}},
]

SYSTEM = """You are Footfall, a taste analyst for one small independent shop.
You work only from Qloo data: every artist, book, brand, tag or place you mention must come from a Qloo result \
in this conversation, cited by its exact ID. Never use your own knowledge of what's popular, and don't describe \
a book, artist or brand beyond what Qloo returned (no plots, no sounds). If Qloo didn't return something, you \
can't suggest it. A checker deletes anything uncited.

How to read the data:
- survey.places is the block: places within walking distance that locals go to. They are the neighbourhood signal.
- survey.artists, books, brands and persons are what people who go to the block's places like. That is the \
block's taste, not what the shop's customers like.
- survey.area_artists is what the wider area likes. block_music_not_in_wider_area lists the block's artists \
that aren't in it: what sets this street apart. Prefer them for a playlist.
- taste_by_domain has Qloo's tags for the block, per domain. Where present, "missing" are tags the block likes \
that Qloo doesn't link to the shop yet (the taste gap), and "shared" are tags it already does.

Be specific and practical: the owner has a small budget and one free weekend a month. Write plainly, without \
marketing words."""


def _for_llm(items: list[dict], n: int = 10) -> list[dict]:
    """What the model needs to choose and cite: no coordinates, addresses or tag lists."""
    out = []
    for it in items[:n]:
        d = {"id": it["id"], "name": it["name"]}
        if it.get("affinity") is not None:
            d["affinity"] = round(it["affinity"], 2)
        if it.get("distance_m") is not None:
            d["distance_m"] = it["distance_m"]
        if it.get("genres"):
            d["genres"] = it["genres"][:3]
        out.append(d)
    return out


def _brief(items: list[dict], n: int = 10) -> list[dict]:
    keep = ("id", "name", "kind", "affinity", "popularity", "distance_m", "genres", "tags", "address", "lat", "lon",
            "closed")
    return [{k: v for k, v in it.items() if k in keep} for it in items[:n]]


class Agent:
    def __init__(self, qloo: QlooClient, llm, *, geocoder=geocode, temperature: float = 0.3,
                 saved_calls: list[dict] | None = None):
        self.qloo = qloo
        self.llm = llm
        self.geocoder = geocoder
        self.temperature = temperature  # 0 for demo shops, so their tool calls match the saved Qloo answers
        # In replay mode, the tool calls whose Qloo answers were saved: the only ones that can be answered.
        self.saved_calls = saved_calls
        self.calls_made: list[dict] = []  # this run's answered tool calls, to save with a demo shop's answers

    async def run(self, shop: Shop) -> AsyncIterator[dict]:
        kind = KINDS.get(shop.kind, KINDS["other"])
        ledger = Ledger()
        tools = Tools(self.qloo, ledger)

        # 1. Locate --------------------------------------------------------
        own = None
        if shop.lat is None or shop.lon is None:
            yield {"type": "step", "stage": "locate", "text": f"Finding {shop.address} on the map"}
            try:
                g = await self.geocoder(shop.address)
                shop.lat, shop.lon = g["lat"], g["lon"]
                label = g["label"]
            except GeocodeError as e:
                yield {"type": "step", "stage": "locate",
                       "text": f"The map service didn't answer ({e}). Asking Qloo where {shop.name} is"}
                me = await tools.find_entity(shop.name, types=["urn:entity:place"], near=False, take=10,
                                             purpose="find the shop on Qloo (the geocoder failed)")
                yield {"type": "qloo", **_call_event(me, "The shop on Qloo")}
                own = next((m for m in me["items"] if same_place(shop.name, m["name"]) and m.get("lat") is not None
                            and address_overlap(shop.address, m.get("address") or "") >= 2), None)
                if not own:
                    yield {"type": "error", "text": f"Couldn't place {shop.address!r} on the map, and Qloo has no "
                                                    f"{shop.name!r} at that address. Check the address and try again."}
                    return
                shop.lat, shop.lon, label = own["lat"], own["lon"], own.get("address") or shop.address
        else:
            label = shop.address
        tools.origin = (shop.lat, shop.lon)
        yield {"type": "located", "lat": shop.lat, "lon": shop.lon, "label": label}

        if own is None:
            me = await tools.find_entity(shop.name, types=["urn:entity:place"], take=5,
                                         purpose="is the shop itself on Qloo?")
            yield {"type": "qloo", **_call_event(me, "The shop on Qloo")}
            # Chains have several branches with the same name: take the nearest.
            own = min((m for m in me["items"] if same_place(shop.name, m["name"])
                       and (m.get("distance_m") or 1e9) <= 3000), key=lambda m: m["distance_m"], default=None)
            if own and own.get("lat") is not None and own.get("distance_m", 0) > 30:
                # Qloo's pin for the shop beats a geocoded street or district.
                shop.lat, shop.lon = own["lat"], own["lon"]
                tools.origin = (shop.lat, shop.lon)
                yield {"type": "located", "lat": shop.lat, "lon": shop.lon,
                       "label": f"{own['name']}, {own.get('address') or label} (Qloo's location for the shop)"}

        # 2. Survey --------------------------------------------------------
        yield {"type": "step", "stage": "survey", "text": f"Asking Qloo which places within {shop.radius_m} m locals go to"}
        near = await tools.places_near(radius_m=shop.radius_m, take=25, purpose="the block: places within walking distance")
        yield {"type": "qloo", **_call_event(near, "Places around the shop")}
        places = [p for p in near["items"] if p.get("kind") == "urn:entity:place" and not p.get("closed")]
        # The neighbourhood signal: the most popular nearby places where taste shows, not the shop itself.
        candidates = [p["id"] for p in sorted(places, key=lambda p: -(p.get("popularity") or 0))
                      if (not own or p["id"] != own["id"]) and is_taste_place(p)]
        signal = candidates[:SIGNAL_SIZE]
        if not signal:
            yield {"type": "error", "text": "Qloo returned no places around this address, so there is no local "
                                            "taste to work from. Try a busier street or a larger radius."}
            return
        skipped = len([p for p in places if not is_taste_place(p)])
        if skipped:
            yield {"type": "step", "stage": "survey",
                   "text": f"Left {skipped} hotels, offices and the like out of the block's signal"}

        survey = {"places": near}
        yield {"type": "step", "stage": "survey", "text": "Carrying the block's taste over to music, books and brands"}
        survey["artists"] = await tools.recommend("urn:entity:artist", entities=signal, take=12,
                                                  purpose="music the people who go to these places like")
        yield {"type": "qloo", **_call_event(survey["artists"], "Music the block likes")}
        signal = _drop_unknown(signal, survey["artists"], candidates)

        survey["area_artists"] = await tools.recommend(
            "urn:entity:artist", near_shop=True, take=12,
            purpose="music the wider area likes (signal.location): the baseline the block is compared with")
        yield {"type": "qloo", **_call_event(survey["area_artists"], "Music the wider area likes")}
        area_ids = {a["id"] for a in survey["area_artists"]["items"]}
        distinctive = [a for a in survey["artists"]["items"] if a["id"] not in area_ids]

        for t in kind["stock_types"]:
            if t == "urn:entity:artist":
                continue
            r = await tools.recommend(t, entities=signal, take=10, purpose=f"{t.split(':')[-1]}s the block likes")
            survey[t] = r
            yield {"type": "qloo", **_call_event(r, f"{t.split(':')[-1].title()}s the block likes")}
            signal = _drop_unknown(signal, r)

        taste = []
        for d in kind["taste"]:
            filt = {"tag_types": d.get("tag_types"), "parents_types": d.get("parents_types")}
            block = await tools.taste_tags(signal, take=15, purpose=f"the block's taste in {d['label'].lower()}", **filt)
            yield {"type": "qloo", **_call_event(block, f"The block's taste: {d['label'].lower()}")}
            artist_ids = [a["id"] for a in survey["artists"]["items"][:SIGNAL_SIZE]]
            if not block["items"] and d.get("via_artists") and artist_ids:
                block = await tools.taste_tags(artist_ids, take=15, purpose="the block's music genres, read off the "
                                                                            "artists its places point to", **filt)
                yield {"type": "qloo", **_call_event(block, "The block's taste: music, via its artists")}
            survey[f"taste_{d['label'].lower()}"] = block
            entry = {"label": d["label"], "block": _brief(block["items"], 15), "missing": None, "shared": None}
            if own:
                mine = await tools.taste_tags([own["id"]], take=25, purpose=f"the shop's own taste in "
                                                                            f"{d['label'].lower()}", **filt)
                yield {"type": "qloo", **_call_event(mine, f"The shop's own taste: {d['label'].lower()}")}
                survey[f"own_taste_{d['label'].lower()}"] = mine
                ids = {t["id"] for t in mine["items"]}
                if ids:  # no tags for the shop means no comparison, not "everything is missing"
                    entry["missing"] = [t for t in entry["block"] if t["id"] not in ids][:10]
                    entry["shared"] = [t for t in entry["block"] if t["id"] in ids][:10]
            taste.append(entry)
        yield {"type": "taste", "on_qloo": bool(own), "domains": taste,
               "distinctive": _brief(distinctive, 8), "signal_size": len(signal)}

        # 3. Investigate ---------------------------------------------------
        yield {"type": "step", "stage": "investigate", "text": f"{self.llm.label} is digging further"}
        brief = {
            "shop": {"name": shop.name, "kind": kind["label"], "sells": shop.sells, "address": label,
                     "on_qloo": own and {"id": own["id"], "name": own["name"]}},
            "can_change": kind["areas"],
            "neighbourhood_signal_ids": signal,
            "survey": {k: {"ref": v["ref"], "items": _for_llm(v["items"])} for k, v in survey.items()
                       if not k.startswith(("taste_", "own_taste_"))},
            "block_music_not_in_wider_area": [a["id"] for a in distinctive],
            "taste_by_domain": [{k: (_for_llm(v, 15) if isinstance(v, list) else v) for k, v in d.items()
                                 if v is not None} for d in taste],
        }
        ask = (f"Here is what Qloo returned so far. Make up to {MAX_TOOL_CALLS} more Qloo calls if they would "
               "sharpen the suggestions (for example tag IDs for what the shop sells, or a heatmap to place the "
               "event). Reply DONE when you have enough.")
        if self.saved_calls is not None:
            ask += ("\n\nQloo is in replay mode: its hackathon API key has expired, so this run uses Qloo answers "
                    "saved earlier. Only the calls listed in saved_calls can be answered. Choose from them, copying "
                    "the name and arguments exactly, or reply DONE." if self.saved_calls else
                    "\n\nQloo is in replay mode and no further calls can be answered: reply DONE.")
            brief["saved_calls"] = self.saved_calls
        messages: list[dict] = [{"role": "system", "content": SYSTEM},
                                {"role": "user", "content": ask + "\n\n" + json.dumps(brief)}]
        used = 0
        while used < MAX_TOOL_CALLS:
            try:
                reply = await self.llm.chat(messages, TOOL_SPECS, temperature=self.temperature)
            except BudgetError:
                yield {"type": "step", "stage": "investigate",
                       "text": "Stopping the digging here: this run reached its spending ceiling"}
                break
            if not reply.tool_calls:
                break
            messages.append({"role": "assistant", "content": reply.content or "",
                             "tool_calls": [{"id": c["id"], "type": "function",
                                             "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])}}
                                            for c in reply.tool_calls]})
            for c in reply.tool_calls:
                used += 1
                result = await self._dispatch(tools, c["name"], c["arguments"])
                if not result.get("error"):
                    self.calls_made.append({"name": c["name"], "arguments": c["arguments"]})
                yield {"type": "qloo", "by": "agent", "why": c["arguments"].get("why", ""),
                       **_call_event(result, c["name"])}
                messages.append({"role": "tool", "tool_call_id": c["id"],
                                 "content": json.dumps({k: (_for_llm(v) if k == "items" else v)
                                                        for k, v in result.items()})})

        # 4. Write ---------------------------------------------------------
        yield {"type": "step", "stage": "write", "text": "Drafting suggestions"}
        messages.append({"role": "user", "content": WRITE_PROMPT.format(areas=", ".join(kind["areas"]))})
        # Thinking off: with it on, Nemotron spent all its tokens reasoning and wrote no
        # JSON. JSON mode off too: with it on, the reply ran to the token limit (10-27).
        reply = await self.llm.chat(messages, None, max_tokens=WRITE_MAX_TOKENS, think=False,
                                    model=self.llm.write_model, temperature=self.temperature)
        try:
            draft = extract_json(reply.content)
        except ValueError:
            messages.append({"role": "assistant", "content": reply.content[:2000]})
            messages.append({"role": "user", "content": "That wasn't a complete JSON object. Reply with the JSON "
                                                        "object only, shorter."})
            reply = await self.llm.chat(messages, None, max_tokens=WRITE_MAX_TOKENS, think=False,
                                        model=self.llm.write_model, temperature=self.temperature)
            draft = extract_json(reply.content)

        # 5. Check ---------------------------------------------------------
        yield {"type": "step", "stage": "check", "text": "Checking every claim against what Qloo returned"}
        plan = checker.check(draft, ledger)
        yield {"type": "plan", "plan": plan, "ledger": ledger.summary(),
               "stats": {"qloo_calls": len(ledger.calls), "agent_calls": used,
                         "llm": self.llm.label, "tokens": dict(self.llm.tokens)}}

    async def _dispatch(self, tools: Tools, name: str, a: dict) -> dict:
        why = a.get("why") or name
        try:
            if name == "recommend":
                return await tools.recommend(a["filter_type"], entities=a.get("entities"), tags=a.get("tags"),
                                             take=int(a.get("take") or 10), purpose=why)
            if name == "find_tags":
                return await tools.find_tags(a["query"], parents_types=a.get("parents_types"), purpose=why)
            if name == "places_near":
                return await tools.places_near(radius_m=int(a.get("radius_m") or 1200), tags=a.get("tags"),
                                               entities=a.get("entities"), purpose=why)
            if name == "heatmap":
                return await tools.heatmap(entities=a.get("entities"), tags=a.get("tags"), purpose=why)
            if name == "find_entity":
                return await tools.find_entity(a["query"], types=a.get("types"), near=False, purpose=why)
        except (KeyError, ValueError, TypeError) as e:
            return {"ref": None, "error": f"bad arguments: {e}", "items": []}
        return {"ref": None, "error": f"unknown tool {name}", "items": []}


WRITE_PROMPT = """Now write the plan as one JSON object, nothing else:
{{
  "summary": "two sentences on who the block is, in Qloo's terms",
  "suggestions": [
    {{
      "area": one of [{areas}],
      "title": "short and concrete",
      "what": "exactly what to do, in 1-3 sentences",
      "why": "the Qloo finding behind it, in plain words for the owner",
      "items": [{{"id": "Qloo entity ID", "name": "its name"}}],
      "evidence": [{{"id": "Qloo entity, tag or geohash ID"}}]
    }}
  ]
}}
Rules: 3 to 5 suggestions, at most one per area except stock (up to two), and exactly one "event" (one weekend \
event). A playlist's items are 8-12 artists. Stock items are specific books, brands, people or artists to stock. \
Menu items are dishes (tags). Event items may be places or tags. In evidence, cite the tag, place or geohash IDs \
behind the idea. Use only IDs that appear in the Qloo results above, copied exactly. Leave out anything you can't \
cite. Menu and stock ideas should close the taste gap: prefer tags under "missing" to tags under "shared". \
Write the text fields for the shop owner: no IDs, no field names such as taste_by_domain, no affinity scores. IDs \
go in items and evidence. Reply with the JSON object only."""


def _drop_unknown(signal: list[str], result: dict, spares: list[str] | None = None) -> list[str]:
    """Remove signal IDs Qloo warned it didn't recognise; top up from spares once."""
    bad = set(result.get("not_found") or [])
    if not bad:
        return signal
    kept = [s for s in signal if s not in bad]
    for s in spares or []:
        if len(kept) >= SIGNAL_SIZE:
            break
        if s not in bad and s not in kept:
            kept.append(s)
    return kept


def _call_event(result: dict, title: str) -> dict:
    return {"title": title, "ref": result.get("ref"), "error": result.get("error"),
            "not_found": len(result.get("not_found") or []),
            "count": len(result.get("items") or []), "items": _brief(result.get("items") or [], 8)}
