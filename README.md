# Footfall

**A taste agent for small local shops.** A café, bookshop or record-shop owner types in their address and what they sell. Footfall asks [Qloo](https://www.qloo.com) what the people around that address like, across music, books, brands and places. It comes back with a short list of concrete changes: a playlist, things to stock or add to the menu, and one weekend event. Each suggestion shows the Qloo results it rests on, and a checker deletes anything Qloo didn't actually return.

Built for the [Qloo Agentic Hackathon](https://qloo.devpost.com) (2026). Status: in development; the live demo link goes here once deployed.

## The problem

Chains run audience research before they choose a playlist or a product line. The independent café on the corner guesses. The owner knows their regulars but not the street: the people who walk past, go to the bar two doors down and the cinema round the corner, and never come in. Footfall gives that owner the street's taste in a form they can act on by Monday.

## How it uses Qloo

Qloo's `/v2/insights` filters by location only for **places** (and destinations and heatmaps): `filter.location` with a radius is documented for `urn:entity:place`, not for artists, books or brands. Footfall uses that gap as its method:

1. **The block.** `filter.type=urn:entity:place`, `filter.location=POINT(lon lat)`, `filter.location.radius` = a walk from the shop. These are the places locals actually go to.
2. **Carry the block's taste across domains.** Every entity type accepts entity IDs as `signal.interests.entities`. The block's most popular places become the signal for `urn:entity:artist`, `urn:entity:book` and `urn:entity:brand`: what do people who go to *these* places listen to, read and buy?
3. **Taste gap.** `filter.type=urn:tag` for the block's places, then again for the shop itself (if Qloo knows it, found with `/search` near the address). The difference is the list of things the street likes that the shop doesn't show yet.
4. **The agent digs further.** An LLM (NVIDIA Nemotron on Nebius Token Factory) gets the survey and makes up to 8 more Qloo calls of its own: `/v2/tags` to find tag IDs for what the shop sells, `urn:heatmap` around the shop to see where an event's audience lives, more cross-domain recommendations.
5. **Write, then check.** The LLM drafts suggestions that must cite Qloo IDs. The checker matches every cited artist, book, brand, tag or heatmap cell against the **evidence ledger** (every Qloo call and everything it returned). An item with an ID Qloo never returned, or a real ID under the wrong name, is cut. A suggestion with no evidence left is dropped, and the page says why.

Two guards against Qloo's "invalid parameters are silently ignored" behaviour:

- `footfall/params.py` holds the Entity Type Parameter Guide as a table. The client refuses any request with a parameter not documented for its `filter.type`, so a typo fails loudly instead of returning plausible, unfiltered results.
- Parameters shown on Qloo's use-case pages but missing from that guide (`signal.location` on artists, books, tags) are allowed only in probe mode. `scripts/probe_location.py` checks whether they change results at two far-apart points before the app relies on them.

## Run it

```bash
pip install -e ".[dev]"
export QLOO_HACK_API_KEY=...        # Qloo hackathon key (hackathon.api.qloo.com only)
export NEBIUS_HACK_API_KEY=...      # Nebius Token Factory key
uvicorn footfall.app:app --reload   # http://localhost:8000
pytest -q                           # no keys needed
```

| Variable | What it does |
|---|---|
| `QLOO_HACK_API_KEY` | Qloo API key, sent as `X-Api-Key` to `https://hackathon.api.qloo.com` |
| `LLM_PROVIDER` | `nebius` (default) or `openai` (any OpenAI-compatible server) |
| `NEBIUS_HACK_API_KEY` | Token Factory key for `https://api.tokenfactory.nebius.com/v1/` |
| `NEBIUS_MODEL` | Nemotron model ID on Token Factory (default `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B`) |
| `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` | for `LLM_PROVIDER=openai` |
| `FOOTFALL_CACHE_DIR` | optional on-disk cache of Qloo responses (private, git-ignored) |

## Layout

| Path | What's in it |
|---|---|
| `footfall/qloo.py` | Qloo client (`/v2/insights`, `/search`, `/v2/tags`), response parsers, the agent's Qloo tools |
| `footfall/params.py` | supported parameters per `filter.type`, checked before every request |
| `footfall/agent.py` | the agent: locate, survey, investigate (tool calls), write |
| `footfall/checker.py` | cuts anything not in the evidence ledger |
| `footfall/evidence.py` | the evidence ledger |
| `footfall/llm.py` | provider switch, one OpenAI-compatible chat call |
| `footfall/geocode.py` | address to coordinates (OpenStreetMap Nominatim) |
| `static/` | the web page (vanilla JS, Leaflet map) |
| `tests/` | tests on synthetic fixtures |

## Data

Test fixtures in `tests/fixtures/` are **made up**, shaped like the examples on docs.qloo.com. No real Qloo response is stored in this repository, as Qloo's terms require; the app caches responses privately on the server only.

Map data © OpenStreetMap contributors. Geocoding by Nominatim, within its usage policy (one request a second, cached).

## Licence

MIT, see [LICENSE](LICENSE).
