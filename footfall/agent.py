"""The Footfall agent.

1. Locate the shop (geocode) and look for it on Qloo.
2. Survey the block: the places within walking distance that locals go to.
   These place IDs become the "neighbourhood signal": Qloo only filters
   places by location, but every entity type accepts place IDs as
   signal.interests.entities, so the block's places can carry its taste
   over to music, books, brands and films.
3. Investigate: the LLM (Nemotron on Token Factory by default) runs its own
   Qloo tool calls on top of the survey, up to MAX_TOOL_CALLS.
4. Write: the LLM drafts a playlist, stock or menu ideas and one weekend
   event, citing Qloo IDs.
5. Check: checker.py cuts anything Qloo didn't return.

run() yields events (dicts) so the UI can show the agent working.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, AsyncIterator

from . import checker
from .evidence import Ledger
from .geocode import geocode
from .llm import extract_json
from .qloo import QlooClient, Tools

MAX_TOOL_CALLS = 8

# What each kind of shop can change, and which Qloo types feed it.
KINDS: dict[str, dict[str, Any]] = {
    "cafe": {"label": "café", "areas": ["playlist", "menu", "stock", "event"],
             "stock_types": ["urn:entity:book", "urn:entity:brand"]},
    "bookshop": {"label": "bookshop", "areas": ["stock", "playlist", "event"],
                 "stock_types": ["urn:entity:book", "urn:entity:person"]},
    "record_shop": {"label": "record shop", "areas": ["stock", "playlist", "event"],
                    "stock_types": ["urn:entity:artist"]},
    "bar": {"label": "bar", "areas": ["playlist", "menu", "event"],
            "stock_types": ["urn:entity:brand"]},
    "other": {"label": "shop", "areas": ["playlist", "stock", "event"],
              "stock_types": ["urn:entity:brand", "urn:entity:book"]},
}


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
in this conversation, cited by its exact ID. Never use your own knowledge of what's popular. If Qloo didn't \
return something, you can't suggest it. A checker deletes anything uncited.
Be specific and practical: the owner has a small budget and one free weekend a month."""


def _brief(items: list[dict], n: int = 10) -> list[dict]:
    keep = ("id", "name", "kind", "affinity", "popularity", "distance_m", "tags", "address", "lat", "lon")
    return [{k: v for k, v in it.items() if k in keep} for it in items[:n]]


class Agent:
    def __init__(self, qloo: QlooClient, llm, *, geocoder=geocode):
        self.qloo = qloo
        self.llm = llm
        self.geocoder = geocoder

    async def run(self, shop: Shop) -> AsyncIterator[dict]:
        kind = KINDS.get(shop.kind, KINDS["other"])
        ledger = Ledger()

        # 1. Locate --------------------------------------------------------
        if shop.lat is None or shop.lon is None:
            yield {"type": "step", "stage": "locate", "text": f"Finding {shop.address} on the map"}
            g = await self.geocoder(shop.address)
            shop.lat, shop.lon = g["lat"], g["lon"]
            label = g["label"]
        else:
            label = shop.address
        yield {"type": "located", "lat": shop.lat, "lon": shop.lon, "label": label}
        tools = Tools(self.qloo, ledger, origin=(shop.lat, shop.lon))

        # 2. Survey --------------------------------------------------------
        yield {"type": "step", "stage": "survey", "text": f"Asking Qloo which places within {shop.radius_m} m locals go to"}
        near = await tools.places_near(radius_m=shop.radius_m, take=25, purpose="the block: places within walking distance")
        yield {"type": "qloo", **_call_event(near, "Places around the shop")}
        places = [p for p in near["items"] if p.get("kind") == "urn:entity:place"] or near["items"]

        me = await tools.find_entity(shop.name, types=["urn:entity:place"], take=3,
                                     purpose="is the shop itself on Qloo?")
        own = next((m for m in me["items"] if (m.get("distance_m") or 1e9) < 250), None)
        yield {"type": "qloo", **_call_event(me, "The shop on Qloo")}

        # The neighbourhood signal: the most popular nearby places, not the shop itself.
        signal = [p["id"] for p in sorted(places, key=lambda p: -(p.get("popularity") or 0))
                  if not own or p["id"] != own["id"]][:8]
        if not signal:
            yield {"type": "error", "text": "Qloo returned no places around this address, so there is no local "
                                            "taste to work from. Try a busier street or a larger radius."}
            return

        survey = {"places": near}
        yield {"type": "step", "stage": "survey", "text": "Carrying the block's taste over to music, books and brands"}
        survey["artists"] = await tools.recommend("urn:entity:artist", entities=signal, take=12,
                                                  purpose="music the people who go to these places like")
        yield {"type": "qloo", **_call_event(survey["artists"], "Music the block likes")}
        for t in kind["stock_types"]:
            if t == "urn:entity:artist":
                continue
            r = await tools.recommend(t, entities=signal, take=10, purpose=f"{t.split(':')[-1]}s the block likes")
            survey[t] = r
            yield {"type": "qloo", **_call_event(r, f"{t.split(':')[-1].title()}s the block likes")}
        survey["block_tags"] = await tools.taste_tags(signal, take=20, purpose="what the block's taste is made of")
        yield {"type": "qloo", **_call_event(survey["block_tags"], "The block's taste, as tags")}

        gap = None
        if own:
            own_tags = await tools.taste_tags([own["id"]], take=20, purpose="what the shop's own taste is made of")
            yield {"type": "qloo", **_call_event(own_tags, "The shop's own taste, as tags")}
            mine = {t["id"] for t in own_tags["items"]}
            gap = {"block_not_shop": [t for t in survey["block_tags"]["items"] if t["id"] not in mine][:10],
                   "shared": [t for t in survey["block_tags"]["items"] if t["id"] in mine][:10]}
            survey["own_tags"] = own_tags
            yield {"type": "gap", **gap}

        # 3. Investigate ---------------------------------------------------
        yield {"type": "step", "stage": "investigate", "text": f"{self.llm.label} is digging further"}
        brief = {
            "shop": {"name": shop.name, "kind": kind["label"], "sells": shop.sells, "address": label,
                     "on_qloo": own and {"id": own["id"], "name": own["name"]}},
            "can_change": kind["areas"],
            "neighbourhood_signal_ids": signal,
            "survey": {k: {"ref": v["ref"], "items": _brief(v["items"])} for k, v in survey.items()},
            "taste_gap": gap and {k: _brief(v) for k, v in gap.items()},
        }
        messages: list[dict] = [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": "Here is what Qloo returned so far. Make up to "
                                        f"{MAX_TOOL_CALLS} more Qloo calls if they would sharpen the suggestions "
                                        "(for example tag IDs for what the shop sells, or a heatmap to place the "
                                        "event). Reply DONE when you have enough.\n\n" + json.dumps(brief)},
        ]
        used = 0
        while used < MAX_TOOL_CALLS:
            reply = await self.llm.chat(messages, TOOL_SPECS)
            if not reply.tool_calls:
                break
            messages.append({"role": "assistant", "content": reply.content or "",
                             "tool_calls": [{"id": c["id"], "type": "function",
                                             "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])}}
                                            for c in reply.tool_calls]})
            for c in reply.tool_calls:
                used += 1
                result = await self._dispatch(tools, c["name"], c["arguments"])
                yield {"type": "qloo", "by": "agent", "why": c["arguments"].get("why", ""),
                       **_call_event(result, c["name"])}
                messages.append({"role": "tool", "tool_call_id": c["id"],
                                 "content": json.dumps({k: (_brief(v) if k == "items" else v)
                                                        for k, v in result.items()})})

        # 4. Write ---------------------------------------------------------
        yield {"type": "step", "stage": "write", "text": "Drafting suggestions"}
        messages.append({"role": "user", "content": WRITE_PROMPT.format(areas=", ".join(kind["areas"]))})
        reply = await self.llm.chat(messages, None, json_mode=True, max_tokens=2500)
        try:
            draft = extract_json(reply.content)
        except ValueError:
            messages.append({"role": "assistant", "content": reply.content})
            messages.append({"role": "user", "content": "That wasn't valid JSON. Reply with the JSON object only."})
            reply = await self.llm.chat(messages, None, json_mode=True, max_tokens=2500)
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
      "why": "the Qloo finding behind it, in plain words",
      "items": [{{"id": "Qloo entity ID", "name": "its name"}}],
      "evidence": [{{"id": "Qloo entity, tag or geohash ID"}}]
    }}
  ]
}}
Rules: 3 to 6 suggestions; exactly one with area "event" (one weekend event). A playlist lists 8-12 artists as items. \
Stock suggestions list the specific books or brands as items. Use only IDs that appear in the Qloo results above, \
copied exactly. Leave out anything you can't cite."""


def _call_event(result: dict, title: str) -> dict:
    return {"title": title, "ref": result.get("ref"), "error": result.get("error"),
            "count": len(result.get("items") or []), "items": _brief(result.get("items") or [], 8)}
