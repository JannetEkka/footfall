"""The evidence ledger: every Qloo call the agent makes, and what it returned.

Suggestions may only cite items in this ledger. The checker (checker.py) uses
it to drop any suggestion whose cited Qloo entity or tag was never returned.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Item:
    """One entity, tag or heatmap cell that Qloo returned."""

    id: str  # Qloo entity_id, tag id, or geohash for heatmap cells
    name: str
    kind: str  # e.g. urn:entity:artist, urn:tag:genre:music, heatmap
    call: str  # the evidence ref of the call that returned it, e.g. "Q3"
    rank: int  # 1-based position in that response
    affinity: float | None = None
    popularity: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def brief(self) -> dict:
        d = {"id": self.id, "name": self.name, "kind": self.kind, "call": self.call, "rank": self.rank}
        if self.affinity is not None:
            d["affinity"] = round(self.affinity, 3)
        if self.popularity is not None:
            d["popularity"] = round(self.popularity, 3)
        for k in ("address", "distance_m", "tags", "lat", "lon"):
            if k in self.extra:
                d[k] = self.extra[k]
        return d


@dataclass
class Call:
    ref: str
    endpoint: str
    params: dict[str, Any]
    purpose: str
    items: list[Item]
    probe: bool = False
    error: str | None = None


class Ledger:
    def __init__(self) -> None:
        self.calls: list[Call] = []
        self._by_id: dict[str, list[Item]] = {}

    def record(self, endpoint: str, params: dict, purpose: str, items: list[dict], *, probe: bool = False,
               error: str | None = None) -> Call:
        ref = f"Q{len(self.calls) + 1}"
        parsed = []
        for rank, raw in enumerate(items, start=1):
            it = Item(call=ref, rank=rank, **raw)
            parsed.append(it)
            self._by_id.setdefault(it.id, []).append(it)
        call = Call(ref, endpoint, dict(params), purpose, parsed, probe=probe, error=error)
        self.calls.append(call)
        return call

    def find(self, item_id: str) -> list[Item]:
        return self._by_id.get(item_id, [])

    def has(self, item_id: str) -> bool:
        return item_id in self._by_id

    def summary(self) -> list[dict]:
        return [
            {
                "ref": c.ref,
                "endpoint": c.endpoint,
                "purpose": c.purpose,
                "params": c.params,
                "probe": c.probe,
                "error": c.error,
                "count": len(c.items),
                "items": [i.brief() for i in c.items],
            }
            for c in self.calls
        ]
