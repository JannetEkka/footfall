"""Address to coordinates, via OpenStreetMap Nominatim.

Nominatim's usage policy: an identifying User-Agent, at most one request a
second, and cache results. One lookup per plan, cached in memory.
"""

from __future__ import annotations

import asyncio
import time

import httpx

URL = "https://nominatim.openstreetmap.org/search"
UA = "footfall/0.1 (Qloo hackathon entry; https://github.com/JannetEkka/footfall)"

_cache: dict[str, dict] = {}
_last = 0.0
_lock = asyncio.Lock()


class GeocodeError(RuntimeError):
    pass


async def geocode(address: str, transport: httpx.AsyncBaseTransport | None = None) -> dict:
    key = " ".join(address.lower().split())
    if key in _cache:
        return _cache[key]
    global _last
    async with _lock:
        wait = 1.0 - (time.monotonic() - _last)
        if wait > 0:
            await asyncio.sleep(wait)
        async with httpx.AsyncClient(transport=transport, timeout=15, headers={"User-Agent": UA}) as http:
            r = await http.get(URL, params={"q": address, "format": "json", "limit": 1, "addressdetails": 1})
        _last = time.monotonic()
    if r.status_code != 200:
        raise GeocodeError(f"geocoder returned {r.status_code}")
    hits = r.json()
    if not hits:
        raise GeocodeError(f"couldn't find {address!r} on the map")
    h = hits[0]
    out = {"lat": float(h["lat"]), "lon": float(h["lon"]), "label": h.get("display_name", address)}
    _cache[key] = out
    return out
