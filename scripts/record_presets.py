"""Save the demo shops' Qloo answers while the Qloo key still works.

Runs each preset through the app itself (same Qloo calls, same Nemotron calls at
temperature 0, same spend ledger), so its pack in the store gets every response
a run needs. Run it twice: the second pass should report no new answers.

    FOOTFALL_STORE_REPO=JannetEkka/footfall-private FOOTFALL_STORE_TOKEN=... \\
        python scripts/record_presets.py [preset ...]

Without FOOTFALL_STORE_REPO the packs go to FOOTFALL_STATE_DIR (default .state/,
git-ignored). Packs are Qloo data: never commit them to a public repo.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("FOOTFALL_VISITOR_RUNS_PER_HOUR", "100")
os.environ.setdefault("FOOTFALL_VISITOR_RUNS_PER_DAY", "100")

from fastapi.testclient import TestClient  # noqa: E402

from footfall import app as app_mod  # noqa: E402
from footfall.replay import PRESETS  # noqa: E402


def main(ids):
    http = TestClient(app_mod.app)
    for pid in ids or PRESETS:
        r = http.post("/api/plan", json={"preset": pid})
        if r.status_code != 200:
            print(f"{pid}: {r.status_code} {r.text[:200]}")
            continue
        evs = [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ")]
        mode = next(e for e in evs if e["type"] == "mode")
        errors = [e.get("text") or e.get("error") for e in evs
                  if e["type"] == "error" or (e["type"] == "qloo" and e.get("error"))]
        plan = next((e for e in evs if e["type"] == "plan"), None)
        if not plan:
            print(f"{pid}: no plan. {errors}")
            continue
        s = plan["stats"]
        print(f"{pid}: {mode['saved']} answers saved before; this run {s['qloo_live']} live, {s['qloo_saved']} "
              f"from the pack; {len(plan['plan']['suggestions'])} suggestions, "
              f"{len(plan['plan']['dropped'])} dropped; LLM ${s['llm_cost_usd']:.4f}")
        for e in errors:
            print("   error:", e)
    print("budget:", http.get("/api/health").json()["budget"])


main(sys.argv[1:])
