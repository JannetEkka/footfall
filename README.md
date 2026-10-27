# Footfall

**A taste agent for small local shops.** A café, bookshop or record-shop owner types in their address and what they sell. Footfall asks [Qloo](https://www.qloo.com) what the people around that address like, across music, books, brands and places. It comes back with a short list of concrete changes: a playlist, things to stock or add to the menu, and one weekend event. Each suggestion shows the Qloo results it rests on, and a checker deletes anything Qloo didn't actually return.

Built for the [Qloo Agentic Hackathon](https://qloo.devpost.com) (2026). Status: in development; the live demo link goes here once deployed.

## The problem

Chains run audience research before they choose a playlist or a product line. The independent café on the corner guesses. The owner knows their regulars but not the street: the people who walk past, go to the bar two doors down and the cinema round the corner, and never come in. Footfall gives that owner the street's taste in a form they can act on by Monday.

## How it uses Qloo

Qloo's `/v2/insights` *filters* by location only for **places** (and destinations and heatmaps): `filter.location` with a radius is documented for `urn:entity:place`, not for artists, books or brands. Footfall uses that gap as its method:

1. **Find the shop.** `/search` for the shop's name near the address, ranked by relevance. The nearest result whose name matches is the shop, and Footfall re-centres on Qloo's pin for it, which is usually more exact than the geocoder's.
2. **The block.** `filter.type=urn:entity:place`, `filter.location=POINT(lon lat)`, `filter.location.radius` = a walk from the shop. These are the places locals actually go to. Hotels, city halls, churches, clinics and the like are left out of the signal: they're popular but say little about taste.
3. **Carry the block's taste across domains.** Every entity type accepts entity IDs as `signal.interests.entities`. The block's places become the signal for `urn:entity:artist`, `urn:entity:book` and `urn:entity:brand`: what do people who go to *these* places listen to, read and buy? When Qloo warns that it doesn't recognise a place ID (`warnings: not_found`), that place is swapped for the next one.
4. **The block against the wider area.** `signal.location` isn't in the Entity Type Parameter Guide, but a live probe showed Qloo honours it on artists, books, brands and tags at neighbourhood scale. Footfall uses it as the baseline: the music the block likes that the wider area doesn't is what makes this street different. The wider area tends towards chart names; the block's places pick out a scene.
5. **Taste gap, one domain at a time.** `filter.type=urn:tag` for the block's places, then for the shop itself, filtered by tag type (`urn:tag:genre:music`, `urn:tag:specialty_dish:place`, book genres). Unfiltered tag results mix in credit cards and hotel ratings. The difference is what the street likes that the shop doesn't show yet. When a block's places have no music tags, the genres are read off the block's artists instead.
6. **The agent digs further.** An LLM (NVIDIA Nemotron on Nebius Token Factory) gets the survey and makes up to 8 more Qloo calls of its own: `/v2/tags` to find tag IDs for what the shop sells, `urn:heatmap` around the shop to see where an event's audience lives, more cross-domain recommendations.
7. **Write, then check.** The LLM drafts suggestions that must cite Qloo IDs. The checker matches every cited artist, book, brand, tag or heatmap cell against the **evidence ledger** (every Qloo call and everything it returned). An item with an ID Qloo never returned, or a real ID under the wrong name, is cut. A suggestion with no evidence left is dropped, and the page says why.

Two guards against Qloo's "invalid parameters are silently ignored" behaviour:

- `footfall/params.py` holds the Entity Type Parameter Guide as a table. The client refuses any request with a parameter not documented for its `filter.type`, so a typo fails loudly instead of returning plausible, unfiltered results.
- Parameters shown on Qloo's use-case pages but missing from that guide are allowed only in probe mode until `scripts/probe_location.py` shows they change results. `signal.location` passed that check for artists, books, brands and tags (10-27). `signal.location.radius` made results *less* local, so it stays probe-only.

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

Test fixtures in `tests/fixtures/` are **made up**, shaped like the examples on docs.qloo.com. No real Qloo response is stored in this repository, as Qloo's terms require; the app caches responses privately on the server only, keeping just the fields it reads.

Map data © OpenStreetMap contributors. Geocoding by Nominatim, within its usage policy (one request a second, cached, one retry). If Nominatim is busy, Footfall asks Qloo's `/search` for the shop and checks the address matches.

## Licence

MIT, see [LICENSE](LICENSE).
