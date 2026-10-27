"""The checker: nothing reaches the shop owner unless Qloo actually returned it.

For each suggestion from the writer:
- every named item (an artist for the playlist, a book or brand to stock) must
  be an entity in the ledger, under that ID, with a matching name, and of a
  kind that fits the area (a playlist is artists, not tags); others are cut;
- every evidence ref must be an ID in the ledger; others are cut;
- a suggestion left with no evidence is dropped, with the reason shown.
"""

from __future__ import annotations

import re

from .evidence import Ledger


# What an item may be, per area. Events can cite anything Qloo returned.
AREA_KINDS = {
    "playlist": ("urn:entity:artist",),
    "stock": ("urn:entity:book", "urn:entity:brand", "urn:entity:person", "urn:entity:artist"),
    "menu": ("urn:tag",),
}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _names_match(claimed: str, actual: str) -> bool:
    a, b = _norm(claimed), _norm(actual)
    return bool(a) and (a == b or a in b or b in a)


def _find(ledger: Ledger, item_id: str):
    """Ledger hits for an ID. A bare geohash is taken as the heatmap cell it names."""
    return ledger.find(item_id) or (ledger.find(f"geohash:{item_id}") if re.fullmatch(r"[0-9a-z]{4,12}", item_id)
                                    else [])


def check(plan: dict, ledger: Ledger) -> dict:
    kept, dropped = [], []
    for s in plan.get("suggestions") or []:
        notes = []
        items_ok = []
        for it in s.get("items") or []:
            iid, name = (it.get("id") or "").strip(), it.get("name") or ""
            hits = _find(ledger, iid)
            if not hits:
                notes.append(f"cut '{name}': Qloo never returned ID {iid or '(none)'}")
                continue
            if not _names_match(name, hits[0].name) and not _names_match(name, hits[0].id):
                notes.append(f"cut '{name}': ID {iid} is '{hits[0].name}' in Qloo's data")
                continue
            kinds = AREA_KINDS.get(s.get("area"))
            if kinds and not hits[0].kind.startswith(kinds):
                notes.append(f"cut '{name}': a {s.get('area')} item can't be a {hits[0].kind}")
                continue
            items_ok.append({**it, "name": hits[0].name, "kind": hits[0].kind, "call": hits[0].call,
                             "rank": hits[0].rank})
        ev_ok = []
        for ref in s.get("evidence") or []:
            rid = ref.get("id") if isinstance(ref, dict) else str(ref)
            hits = _find(ledger, (rid or "").strip())
            if hits:
                ev_ok.append(hits[0].brief())
            else:
                notes.append(f"cut evidence {rid!r}: not in any Qloo response")
        # Items Qloo returned are evidence for the suggestion too.
        seen = {e["id"] for e in ev_ok}
        for it in items_ok:
            if it["id"] not in seen:
                ev_ok.append(_find(ledger, it["id"])[0].brief())
                seen.add(it["id"])
        had_items = bool(s.get("items"))
        if not ev_ok or (had_items and not items_ok):
            dropped.append({"title": s.get("title"), "area": s.get("area"),
                            "reason": "; ".join(notes) or "no Qloo evidence cited"})
            continue
        kept.append({**s, "items": items_ok, "evidence": ev_ok, "checker_notes": notes})
    return {**plan, "suggestions": kept, "dropped": dropped}
