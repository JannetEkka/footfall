"""Live check of Qloo's location parameters. Prints a verdict per question;
stores nothing (Qloo data stays out of the repo).

    python scripts/probe_location.py

1. Do places come back near the point we ask for? (filter.location + radius)
2. Does signal.location change artist / book / brand / tag results, or is it
   silently ignored? Same request at two far-apart points; identical lists = ignored.
3. How local is it? Same request at points 1 km and 7 km apart in one city,
   with and without signal.location.radius.

First run, 2026-10-27: 1 works (all places within 600 m of an 800 m radius);
2 changes results for all four types; 3 is neighbourhood-scale without a
radius (9/10 artists shared at 1 km, 3/10 at 7 km), and adding
signal.location.radius=1000 made results *less* local (8/10 shared at 7 km).
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from footfall.evidence import Ledger  # noqa: E402
from footfall.qloo import QlooClient, Tools, parse_entities, parse_tags  # noqa: E402

A = (51.5246, -0.0786)  # Shoreditch, London
B = (12.9784, 77.6408)  # Indiranagar, Bengaluru
A_1KM = (51.5310, -0.0640)  # Haggerston, about 1.2 km from A
A_7KM = (51.4988, -0.1749)  # South Kensington, about 7.5 km from A


def _point(pt):
    return f"POINT({pt[1]} {pt[0]})"


async def _names(q, ftype, parse, pt, radius=None):
    p = {"filter.type": ftype, "signal.location": _point(pt), "take": 10}
    if radius:
        p["signal.location.radius"] = radius
    try:
        return [i["name"] for i in parse(await q.insights(p, probe=True))]
    except Exception as e:  # noqa: BLE001
        return f"ERROR {e}"


async def main():
    q = QlooClient(cache_dir="")
    if not q.configured:
        sys.exit("QLOO_HACK_API_KEY is not set")
    t = Tools(q, Ledger(), origin=A)
    r = await t.places_near(radius_m=800, take=10)
    if r.get("error"):
        print("1. places_near ERROR:", r["error"])
    else:
        d = [i.get("distance_m") for i in r["items"]]
        print(f"1. places_near: {r['count']} places, distances (m): {d}")
        print("   ->", "LOCATION FILTER WORKS" if d and max(x or 1e9 for x in d) <= 1500 else "CHECK: places far away")

    types = (("urn:entity:artist", parse_entities), ("urn:entity:book", parse_entities),
             ("urn:entity:brand", parse_entities), ("urn:tag", parse_tags))
    for ftype, parse in types:
        lists = [await _names(q, ftype, parse, pt) for pt in (A, B)]
        same = lists[0] == lists[1]
        print(f"2. signal.location on {ftype}: {'IGNORED (same results)' if same else 'CHANGES RESULTS'}")
        print("   London:", lists[0])
        print("   Bengaluru:", lists[1])

    for ftype, parse in (types[0], types[3]):
        for radius in (None, 1000):
            base = await _names(q, ftype, parse, A, radius)
            near = await _names(q, ftype, parse, A_1KM, radius)
            far = await _names(q, ftype, parse, A_7KM, radius)
            if isinstance(base, str):
                print(f"3. {ftype} radius={radius}: {base}")
                continue
            shared = [len(set(base) & set(x)) if isinstance(x, list) else x for x in (near, far)]
            print(f"3. {ftype} radius={radius}: shared with Shoreditch at 1 km: {shared[0]}/10, at 7 km: {shared[1]}/10")
    print(f"requests sent: {q.requests_sent}")
    await q.aclose()


asyncio.run(main())
