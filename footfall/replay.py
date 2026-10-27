"""Demo shops whose Qloo answers are saved, so the demo outlives the Qloo key.

Qloo's hackathon keys stop working after the hackathon; Nebius's judges test
until mid-December. For each preset shop, every Qloo response a run fetches is
kept in a pack (cache key -> slimmed response) in the private store
(store.py, never the public repo: Qloo's terms keep their data out of public
repos, and allow private caching). Preset runs read the pack first and add to
it while the key works; once it doesn't, they run from the pack alone.

Preset runs use temperature 0, but that doesn't make Token Factory's
Nemotron repeat its tool calls exactly (a second run of one shop made 8 new
calls). So the pack also keeps the tool calls Nemotron made, and in replay mode
the agent is told they're the only calls that can be answered, and to copy one
exactly. Nemotron itself always runs live on Token Factory.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from .store import Conflict

PACK_LIMIT_BYTES = 900_000  # the GitHub Contents API returns files up to 1 MB inline

# Coordinates are approximate (OpenStreetMap); the agent re-centres on Qloo's pin for the shop.
PRESETS: dict[str, dict] = {
    "brooklyn-bookshop": {
        "title": "Bookshop · Boerum Hill, Brooklyn",
        "shop": {"name": "Books Are Magic", "address": "225 Smith St, Brooklyn, NY 11231", "kind": "bookshop",
                 "sells": "new fiction and non-fiction, a big children's section, author events",
                 "lat": 40.6839869, "lon": -73.9920368, "radius_m": 1200},
    },
    "shoreditch-cafe": {
        "title": "Café · Shoreditch, London",
        "shop": {"name": "Allpress Espresso", "address": "58 Redchurch Street, London E2 7DP", "kind": "cafe",
                 "sells": "flat whites, filter coffee, toasties, pastries",
                 "lat": 51.5244235, "lon": -0.0742436, "radius_m": 1200},
    },
    "koramangala-cafe": {
        "title": "Café · Koramangala, Bengaluru",
        "shop": {"name": "Dyu Art Cafe", "address": "Koramangala 8th Block, Bengaluru", "kind": "cafe",
                 "sells": "coffee, sandwiches, art on the walls",
                 "lat": 12.9282918, "lon": 77.6254034, "radius_m": 1200},
    },
}


def _call_key(c: dict) -> str:
    return json.dumps([c["name"], {k: v for k, v in c["arguments"].items() if k != "why"}], sort_keys=True)


def pack_name(preset_id: str) -> str:
    if preset_id not in PRESETS:
        raise KeyError(preset_id)
    return f"replay/{preset_id}.json"


async def load_pack(store, preset_id: str) -> dict:
    """{"bodies": {cache key: response}, "agent_calls": [...], "recorded": iso date}; empty if none yet."""
    data, _ = await store.read(pack_name(preset_id))
    data = data or {"bodies": {}, "recorded": None}
    data.setdefault("agent_calls", [])
    return data


async def save_recorded(store, preset_id: str, recorded: dict[str, dict],
                        agent_calls: list[dict] | None = None) -> dict | None:
    """Merge newly fetched responses (and the tool calls behind them) into the
    preset's pack. Returns the new pack, or None if nothing was new or it's full."""
    if not recorded and not agent_calls:
        return None
    for _ in range(4):
        data, version = await store.read(pack_name(preset_id))
        data = data or {"bodies": {}, "recorded": None}
        calls = list(data.get("agent_calls") or [])
        known = {_call_key(c) for c in calls}
        for c in agent_calls or []:
            if _call_key(c) not in known:
                calls.append(c)
                known.add(_call_key(c))
        new = {k: v for k, v in recorded.items() if k not in data["bodies"]}
        if not new and len(calls) == len(data.get("agent_calls") or []):
            return None
        merged = {**data["bodies"], **new}
        pack = {"bodies": merged, "agent_calls": calls,
                "recorded": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "note": "Private Qloo responses for the Footfall demo. Never commit to a public repo."}
        if len(json.dumps(pack)) > PACK_LIMIT_BYTES:
            return None
        try:
            await store.write(pack_name(preset_id), pack, version, f"replay pack {preset_id}: +{len(new)}")
            return pack
        except Conflict:
            await asyncio.sleep(0.3)
    return None
