"""Qloo hackathon API client.

Base URL https://hackathon.api.qloo.com only (other hosts 401 a hackathon key),
key in the X-Api-Key header, GET with query-string parameters.
Every /v2/insights request is checked against params.SUPPORTED before it is
sent, and every response is written to the evidence ledger.

Responses are cached server-side (Qloo's FAQ allows private caching) in memory
and, if FOOTFALL_CACHE_DIR is set, on disk. Only the fields Footfall reads are
kept (slim()), which cuts a place response from about 100 KB to under 10 KB.
The cache must never be committed: Qloo's terms don't allow their data in a
public repo.

Replay: a client can be given a pack of saved responses (replay.py) that it
reads before going live, and it keeps every live response in `recorded` so the
pack can grow. With offline=True it never goes live: Qloo's hackathon keys stop
working after the hackathon, but the demo shops keep working from their packs.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any

import httpx

from . import params as P
from .evidence import Ledger

BASE_URL = "https://hackathon.api.qloo.com"


class QlooError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"Qloo {status}: {message}" if status else message)
        self.status = status


OFFLINE_MISS = "not among the saved Qloo answers (replay mode: Qloo's hackathon key has expired)"


def cache_key(path: str, query: dict[str, Any]) -> tuple[str, dict[str, str]]:
    q = {k: _fmt(v) for k, v in query.items() if v is not None and v != ""}
    return hashlib.sha256(json.dumps([path, sorted(q.items())]).encode()).hexdigest()[:24], q


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _fmt(v: Any) -> str:
    if isinstance(v, (list, tuple)):
        return ",".join(str(x) for x in v)
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


# Places that say little about local taste: they'd dilute the block's signal.
NOT_TASTE = {
    "atm", "bank", "blood testing service", "bus station", "church", "city hall", "clinic", "convenience store",
    "courthouse", "dentist", "doctor", "gas station", "government office", "grocery store", "hospital", "hotel",
    "laboratory", "medical center", "mosque", "motel", "parking", "parking garage", "pharmacy", "police station",
    "post office", "school", "std testing service", "supermarket", "synagogue", "temple", "train station",
    "transit station", "university",
}


def _slim_entity(e: dict) -> dict:
    props = e.get("properties") or {}
    keep_props = {k: props[k] for k in ("address", "is_closed") if k in props}
    tags = e.get("tags") or []
    genres = [t for t in tags if t.get("type") == "urn:tag:genre:place"]
    out = {k: e[k] for k in ("entity_id", "id", "name", "subtype", "type", "types", "popularity", "location")
           if k in e}
    q = e.get("query") or {}
    if q:
        out["query"] = {k: q[k] for k in ("affinity", "distance") if k in q}
    if keep_props:
        out["properties"] = keep_props
    if tags:
        rest = [t for t in tags if t.get("type") != "urn:tag:genre:place"]
        out["tags"] = [{k: t[k] for k in ("id", "tag_id", "name", "type") if t.get(k) is not None}
                       for t in genres[:4] + rest[:8]]
    return out


def _heat_affinity(c: dict) -> float:
    return (c.get("query") or {}).get("affinity") or 0


def slim(body: dict) -> dict:
    """Keep only what the parsers read. Applied before anything is cached."""
    out = {k: body[k] for k in ("success", "warnings") if k in body}
    results = body.get("results")
    if isinstance(results, list):  # /search
        out["results"] = [_slim_entity(e) for e in results]
    elif isinstance(results, dict):
        r: dict[str, Any] = {}
        if "entities" in results:
            r["entities"] = [_slim_entity(e) for e in results["entities"] or []]
        if "tags" in results:
            r["tags"] = [{k: t[k] for k in ("tag_id", "id", "name", "subtype", "type", "popularity", "query")
                          if k in t} for t in results["tags"] or []]
        if "heatmap" in results:  # hundreds of cells; parse_heatmap reads the top 50
            cells = sorted(results["heatmap"] or [], key=_heat_affinity, reverse=True)[:50]
            r["heatmap"] = [{"location": {k: (c.get("location") or {}).get(k) for k in ("latitude", "longitude",
                                                                                       "geohash")},
                             "query": {k: (c.get("query") or {}).get(k) for k in ("affinity", "popularity")}}
                            for c in cells]
        for k, v in results.items():
            r.setdefault(k, v)  # demographics: already small
        out["results"] = r
    else:
        out["results"] = results
    return out


def not_found(body: dict) -> list[str]:
    """Signal IDs Qloo didn't recognise (and so ignored), from the response's warnings."""
    out: list[str] = []
    for w in body.get("warnings") or []:
        if isinstance(w, dict) and w.get("type") == "not_found":
            for k, v in w.items():
                if k.startswith("signal.") and isinstance(v, list):
                    out += [str(x) for x in v]
    return out


def _entity_location(e: dict) -> tuple[float, float] | None:
    loc = e.get("location") or (e.get("properties") or {}).get("location") or {}
    lat = loc.get("lat", loc.get("latitude"))
    lon = loc.get("lon", loc.get("lng", loc.get("longitude")))
    if lat is None or lon is None:
        return None
    return float(lat), float(lon)


def parse_entities(body: dict, origin: tuple[float, float] | None = None) -> list[dict]:
    results = body.get("results")
    if isinstance(results, dict):
        ents = results.get("entities") or []
    elif isinstance(results, list):  # /search returns a bare list
        ents = results
    else:
        ents = []
    out = []
    for e in ents:
        eid = e.get("entity_id") or e.get("id")
        if not eid:
            continue
        q = e.get("query") or {}
        props = e.get("properties") or {}
        extra: dict[str, Any] = {}
        if props.get("address"):
            extra["address"] = props["address"]
        if props.get("is_closed"):
            extra["closed"] = True
        tags = [t.get("name").strip() for t in (e.get("tags") or []) if t.get("name")]
        if tags:
            extra["tags"] = tags[:8]
        genres = [t["name"].strip() for t in e.get("tags") or []
                  if t.get("type") == "urn:tag:genre:place" and t.get("name")]
        if genres:
            extra["genres"] = genres[:4]
        loc = _entity_location(e)
        if loc:
            extra["lat"], extra["lon"] = loc
            if origin:
                extra["distance_m"] = round(haversine_m(origin[0], origin[1], *loc))
        out.append(
            {
                "id": eid,
                "name": (e.get("name") or eid).strip(),
                "kind": e.get("subtype") or e.get("type") or (e.get("types") or ["urn:entity"])[0],
                "affinity": q.get("affinity"),
                "popularity": e.get("popularity"),
                "extra": extra,
            }
        )
    return out


def parse_tags(body: dict) -> list[dict]:
    results = body.get("results") or {}
    tags = results.get("tags") if isinstance(results, dict) else results
    out = []
    for t in tags or []:
        tid = t.get("tag_id") or t.get("id")
        if not tid:
            continue
        q = t.get("query") or {}
        out.append(
            {
                "id": tid,
                "name": (t.get("name") or tid).strip(),
                "kind": t.get("subtype") or t.get("type") or "urn:tag",
                "affinity": q.get("affinity"),
                "popularity": t.get("popularity", q.get("popularity")),
            }
        )
    return out


def parse_heatmap(body: dict) -> list[dict]:
    cells = (body.get("results") or {}).get("heatmap") or []
    out = []
    for c in cells:
        loc = c.get("location") or {}
        q = c.get("query") or {}
        gh = loc.get("geohash")
        if not gh:
            continue
        out.append(
            {
                "id": f"geohash:{gh}",
                "name": gh,
                "kind": "heatmap",
                "affinity": q.get("affinity"),
                "popularity": q.get("popularity"),
                "extra": {"lat": loc.get("latitude"), "lon": loc.get("longitude")},
            }
        )
    out.sort(key=lambda d: -(d["affinity"] or 0))
    return out[:50]  # Qloo returns hundreds of cells whatever `take` says


class QlooClient:
    def __init__(self, api_key: str | None = None, *, base_url: str = BASE_URL,
                 transport: httpx.AsyncBaseTransport | None = None, cache_dir: str | None = None,
                 timeout: float = 20.0, pack: dict[str, dict] | None = None, offline: bool = False):
        self.api_key = api_key if api_key is not None else os.environ.get("QLOO_HACK_API_KEY", "")
        self._http = httpx.AsyncClient(base_url=base_url, transport=transport, timeout=timeout,
                                       headers={"X-Api-Key": self.api_key, "Accept": "application/json"})
        self._mem: dict[str, dict] = {}
        cache_dir = cache_dir if cache_dir is not None else os.environ.get("FOOTFALL_CACHE_DIR")
        self._disk = Path(cache_dir) if cache_dir else None
        if self._disk:
            self._disk.mkdir(parents=True, exist_ok=True)
        self.pack = pack if pack is not None else {}
        self.offline = offline
        self.recorded: dict[str, dict] = {}  # live responses this client fetched
        self.requests_sent = 0
        self.pack_hits = 0
        self.auth_failed = False  # a 401/403: the key has been switched off

    @property
    def configured(self) -> bool:
        return bool(self.api_key) or self.offline

    async def aclose(self) -> None:
        await self._http.aclose()

    async def get(self, path: str, query: dict[str, Any]) -> dict:
        key, q = cache_key(path, query)
        if key in self._mem:
            return self._mem[key]
        if key in self.pack:
            self.pack_hits += 1
            self._mem[key] = self.pack[key]
            return self.pack[key]
        if self._disk and (self._disk / f"{key}.json").exists():
            body = json.loads((self._disk / f"{key}.json").read_text())
            self._mem[key] = body
            return body
        if self.offline:
            raise QlooError(0, OFFLINE_MISS)
        if not self.api_key:
            raise QlooError(0, "QLOO_HACK_API_KEY is not set")
        self.requests_sent += 1
        r = await self._http.get(path, params=q)
        if r.status_code in (401, 403):
            self.auth_failed = True
        if r.status_code >= 400:
            try:
                msg = r.json()
            except ValueError:
                msg = r.text[:300]
            raise QlooError(r.status_code, json.dumps(msg)[:300] if not isinstance(msg, str) else msg)
        body = slim(r.json())
        self._mem[key] = body
        self.recorded[key] = body
        if self._disk:
            (self._disk / f"{key}.json").write_text(json.dumps(body))
        return body

    # --- raw endpoints -------------------------------------------------------

    async def insights(self, params: dict[str, Any], *, probe: bool = False) -> dict:
        P.check(params, probe=probe)
        return await self.get("/v2/insights", params)

    async def search(self, query: str, *, types: list[str] | None = None, location: tuple[float, float] | None = None,
                     radius_miles: float | None = None, take: int = 10, sort_by: str | None = None) -> dict:
        q: dict[str, Any] = {"query": query, "take": take}
        if types:
            q["types"] = types
        if location:
            q["filter.location"] = f"{location[0]},{location[1]}"
        if radius_miles is not None:
            q["filter.radius"] = radius_miles
        if sort_by:
            q["sort_by"] = sort_by
        return await self.get("/search", q)

    async def tags(self, query: str, *, tag_types: list[str] | None = None, parents_types: list[str] | None = None,
                   take: int = 10, semantic: bool = True) -> dict:
        q: dict[str, Any] = {"filter.query": query, "take": take}
        if tag_types:
            q["filter.tag.types"] = tag_types
        if parents_types:
            q["filter.parents.types"] = parents_types
        if semantic:
            q["feature.semantic_search"] = True
        return await self.get("/v2/tags", q)

    async def compare(self, a_entities: list[str], b_entities: list[str], *, filter_type: str | None = None,
                      take: int = 20) -> dict:
        q: dict[str, Any] = {"a.signal.interests.entities": a_entities, "b.signal.interests.entities": b_entities,
                             "take": take}
        if filter_type:
            q["filter.type"] = filter_type
        return await self.get("/v2/analysis/compare", q)


class Tools:
    """The Qloo calls the agent can make. Each one records to the ledger and
    returns a compact, LLM-readable result that carries the evidence refs."""

    def __init__(self, client: QlooClient, ledger: Ledger, origin: tuple[float, float] | None = None):
        self.q = client
        self.ledger = ledger
        self.origin = origin

    def _point(self) -> str:
        if not self.origin:
            raise ValueError("no location set")
        return f"POINT({self.origin[1]} {self.origin[0]})"  # WKT is lon lat

    async def _run(self, endpoint: str, params: dict, purpose: str, coro, parser, probe=False) -> dict:
        try:
            body = await coro
        except (QlooError, P.UnsupportedParameter, httpx.HTTPError) as e:
            msg = ("Qloo took too long to answer" if isinstance(e, httpx.TimeoutException)
                   else str(e) or type(e).__name__)  # httpx timeouts have an empty message
            call = self.ledger.record(endpoint, params, purpose, [], probe=probe, error=msg)
            return {"ref": call.ref, "error": msg, "items": []}
        missing = not_found(body)
        call = self.ledger.record(endpoint, params, purpose, parser(body), probe=probe, not_found=missing)
        out = {"ref": call.ref, "count": len(call.items), "items": [i.brief() for i in call.items]}
        if missing:
            out["not_found"] = missing
        return out

    async def find_entity(self, query: str, types: list[str] | None = None, near: bool = True, take: int = 5,
                          purpose: str = "look up an entity") -> dict:
        # Ranked by relevance, not distance: sort_by=distance returns whatever is
        # nearest, whatever its name (first live run, 10-27).
        loc = self.origin if near else None
        params = {"query": query, "types": types, "near": bool(loc)}
        return await self._run("/search", params, purpose,
                               self.q.search(query, types=types, location=loc, radius_miles=2 if loc else None,
                                             take=take),
                               lambda b: parse_entities(b, self.origin))

    async def find_tags(self, query: str, parents_types: list[str] | None = None, take: int = 8,
                        purpose: str = "find tag IDs") -> dict:
        params = {"filter.query": query, "filter.parents.types": parents_types}
        return await self._run("/v2/tags", params, purpose,
                               self.q.tags(query, parents_types=parents_types, take=take), parse_tags)

    async def places_near(self, radius_m: int = 1200, tags: list[str] | None = None, entities: list[str] | None = None,
                          take: int = 20, purpose: str = "places around the shop") -> dict:
        params: dict[str, Any] = {"filter.type": "urn:entity:place", "filter.location": self._point(),
                                  "filter.location.radius": radius_m, "take": take}
        if tags:
            params["filter.tags"] = tags
        if entities:
            params["signal.interests.entities"] = entities
        if not tags and not entities:
            # Insights needs at least one signal or filter besides the location;
            # popularity keeps it to places locals actually go to.
            params["filter.popularity.min"] = 0.3
        return await self._run("/v2/insights", params, purpose, self.q.insights(params),
                               lambda b: parse_entities(b, self.origin))

    async def recommend(self, filter_type: str, entities: list[str] | None = None, tags: list[str] | None = None,
                        take: int = 10, near_shop: bool = False, extra: dict | None = None, probe: bool = False,
                        purpose: str = "recommendations") -> dict:
        params: dict[str, Any] = {"filter.type": filter_type, "take": take}
        if entities:
            params["signal.interests.entities"] = entities
        if tags:
            params["signal.interests.tags"] = tags
        if near_shop:
            # Neighbourhood-scale; no radius, which made results less local (probe, 10-27).
            params["signal.location"] = self._point()
        params.update(extra or {})
        return await self._run("/v2/insights", params, purpose, self.q.insights(params, probe=probe),
                               lambda b: parse_entities(b, self.origin), probe=probe)

    async def taste_tags(self, entities: list[str], parents_types: list[str] | None = None,
                         tag_types: list[str] | None = None, take: int = 15,
                         purpose: str = "what the neighbourhood's taste is made of") -> dict:
        params: dict[str, Any] = {"filter.type": "urn:tag", "signal.interests.entities": entities, "take": take}
        if parents_types:
            params["filter.parents.types"] = parents_types
        if tag_types:
            params["filter.tag.types"] = tag_types
        return await self._run("/v2/insights", params, purpose, self.q.insights(params), parse_tags)

    async def heatmap(self, entities: list[str] | None = None, tags: list[str] | None = None, radius_m: int = 3000,
                      take: int = 40, purpose: str = "where this taste is strongest around the shop") -> dict:
        params: dict[str, Any] = {"filter.type": "urn:heatmap", "filter.location": self._point(),
                                  "filter.location.radius": radius_m, "take": take}
        if entities:
            params["signal.interests.entities"] = entities
        if tags:
            params["signal.interests.tags"] = tags
        return await self._run("/v2/insights", params, purpose, self.q.insights(params), parse_heatmap)

    async def demographics(self, entities: list[str], purpose: str = "who the nearby places draw") -> dict:
        params = {"filter.type": "urn:demographics", "signal.interests.entities": entities}

        def parse(body):
            out = []
            for d in (body.get("results") or {}).get("demographics") or []:
                q = d.get("query") or {}
                ages = q.get("age") or {}
                top = max(ages, key=ages.get) if ages else None
                out.append({"id": f"demo:{d.get('entity_id')}", "name": f"strongest age band: {top}",
                            "kind": "demographics", "extra": {"age": ages, "gender": q.get("gender")}})
            return out

        return await self._run("/v2/insights", params, purpose, self.q.insights(params), parse)
