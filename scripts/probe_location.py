"""First live check once QLOO_HACK_API_KEY is set. Prints a verdict per question;
stores nothing (Qloo data stays out of the repo).

    python scripts/probe_location.py

1. Do places come back near the point we ask for? (filter.location + radius)
2. Does signal.location change artist / book / tag results, or is it silently
   ignored? Same request at two far-apart points; identical lists = ignored.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from footfall.evidence import Ledger  # noqa: E402
from footfall.qloo import QlooClient, Tools, parse_entities, parse_tags  # noqa: E402

A = (51.5246, -0.0786)  # Shoreditch, London
B = (12.9784, 77.6408)  # Indiranagar, Bengaluru


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

    for ftype, parse in (("urn:entity:artist", parse_entities), ("urn:entity:book", parse_entities),
                         ("urn:tag", parse_tags)):
        lists = []
        for pt in (A, B):
            p = {"filter.type": ftype, "signal.location": f"POINT({pt[1]} {pt[0]})", "take": 10}
            try:
                body = await q.insights(p, probe=True)
                lists.append([i["name"] for i in parse(body)])
            except Exception as e:  # noqa: BLE001
                lists.append(f"ERROR {e}")
        same = lists[0] == lists[1]
        print(f"2. signal.location on {ftype}: {'IGNORED (same results)' if same else 'CHANGES RESULTS'}")
        print("   London:", lists[0])
        print("   Bengaluru:", lists[1])
    print(f"requests sent: {q.requests_sent}")
    await q.aclose()


asyncio.run(main())
