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

## Nemotron and Token Factory

The agent runs on two NVIDIA Nemotron models, served by **Nebius Token Factory** through its OpenAI-compatible API (`footfall/llm.py`):

- **Investigate, with Nemotron 3.5 Lightning** (`nvidia/Nemotron-3_5-Lightning`): fast, cheap tool calling. It gets the survey (the block's places, their music, books and brands, the wider-area baseline and the taste gap) and Footfall's five Qloo tools as function definitions. It decides which further Qloo calls would sharpen the plan, up to 8: tag IDs for what the shop sells, a heatmap to place the event, more cross-domain recommendations. Each call and its reason show in the live log.
- **Write, with Nemotron 3 Super 120B** (`nvidia/nemotron-3-super-120b-a12b`). It drafts the plan as JSON citing Qloo IDs. The checker then cuts anything Qloo didn't return.

Where Token Factory helped:

- `GET /v1/models?verbose=true` lists each model's tool support and per-token prices. Footfall chose its model from that list and reads the prices at startup to cost every call.
- **Picking the tool caller.** The cheapest Nemotrons, Nano 30B-A3B and 3.5 Lightning, both cost $0.06/M input and $0.24/M output tokens. Both made correct parallel tool calls on the first try. Lightning answered the same tool-call test in 1.6 s with 118 tokens; Nano took 7.4 s and 483 tokens.
- **Picking the writer.** On identical replayed Qloo data, Lightning kept 2 of 5 suggestions for the Brooklyn shop (it listed tags as books to stock) and misread the Shoreditch taste gap. Super kept 4 of 4 and 5 of 5 and read the gap right. Ultra 550B wrote the most detail, but put IDs into the text, at 2–3× Super's cost.
- **Cost.** A full run (9 LLM calls, about 80k input tokens) costs about **$0.013** and takes 20–30 s.
- `chat_template_kwargs: {"enable_thinking": false}` matters for the write step. With thinking on, Nemotron spent its whole token allowance reasoning and wrote no JSON. Tool rounds keep the default.

### Spend cap

The Token Factory account has a card on file and no spending limit, and the demo is public for weeks. So Footfall caps spending itself (`footfall/budget.py`):

- **Reserve, then settle.** Each run reserves a ceiling ($0.05) in a spend ledger before the first LLM call; if that would pass the total cap (`FOOTFALL_BUDGET_USD`, default $15), or the day's run limit (20), the run doesn't start. At about $0.015 a run, 20 runs a day can't use up $15 before judging ends on Dec 15. Afterwards the reservation is replaced by the actual cost. If the server dies mid-run, the whole reservation stays counted.
- **Check every call before sending it.** Its worst case (prompt bytes ÷ 2 as tokens, plus `max_tokens`) must fit in what's left of the run's reservation. Then it's priced from the usage Token Factory reports, with a 25% margin.
- **A durable ledger.** It lives in a private GitHub repo (`FOOTFALL_STORE_REPO`, written through the Contents API) because Render's disk is wiped on every restart. On Render, a ledger that isn't durable means no LLM calls at all.
- **Visitor limits.** 4 runs an hour and 12 a day per visitor, 2 at once.

## Demo shops and replay mode

Qloo's hackathon API keys are switched off after the hackathon, but the demo has to keep working for judges until mid-December. So the three demo shops on the page (a bookshop in Brooklyn, cafés in Shoreditch and Koramangala) keep their Qloo answers (`footfall/replay.py`):

- While the key works, a demo-shop run reads its saved answers first and saves any new ones, together with the tool calls Nemotron made. The answers are slimmed to the fields Footfall reads and kept in the same **private** store as the spend ledger, never in this repo. Qloo allows private caching; their terms keep their data out of public repos.
- When Qloo answers 401/403 (or `FOOTFALL_QLOO_OFFLINE=1`), the app switches to replay. Demo shops run from their saved answers; your own address is paused, and the page says why.
- Nemotron always runs live. Even at temperature 0, Token Factory's Nemotron doesn't repeat its tool calls exactly, so in replay it is shown the saved calls and told they're the only ones Qloo can answer. On the first replay test, all 18–19 Qloo answers per run came from the saved answers, with no misses.

`scripts/record_presets.py` runs each demo shop through the app to save its answers; run it twice while the key works.

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
| `NEBIUS_MODEL` | Nemotron for the tool-calling rounds (default `nvidia/Nemotron-3_5-Lightning`) |
| `NEBIUS_WRITE_MODEL` | Nemotron for the write-up (default `nvidia/nemotron-3-super-120b-a12b`) |
| `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` | for `LLM_PROVIDER=openai` |
| `FOOTFALL_CACHE_DIR` | optional on-disk cache of Qloo responses (private, git-ignored) |
| `FOOTFALL_STORE_REPO`, `FOOTFALL_STORE_TOKEN` | private GitHub repo for the spend ledger and replay packs, and a fine-grained token with Contents read/write on that repo only. Required on Render |
| `FOOTFALL_STATE_DIR` | local folder for the same, when no repo is set (default `.state/`, git-ignored) |
| `FOOTFALL_QLOO_OFFLINE` | `1` to run only the demo shops, from their saved Qloo answers (automatic after a 401/403) |
| `FOOTFALL_BUDGET_USD` | total LLM spend cap (default `15`) |
| `FOOTFALL_RUN_CEILING_USD` | most one run may spend (default `0.05`) |
| `FOOTFALL_RUNS_PER_DAY` | runs a day, all visitors (default `20`) |
| `FOOTFALL_VISITOR_RUNS_PER_HOUR`, `FOOTFALL_VISITOR_RUNS_PER_DAY`, `FOOTFALL_CONCURRENT_RUNS` | visitor limits (defaults `4`, `12`, `2`) |

## Layout

| Path | What's in it |
|---|---|
| `footfall/qloo.py` | Qloo client (`/v2/insights`, `/search`, `/v2/tags`), response parsers, the agent's Qloo tools |
| `footfall/params.py` | supported parameters per `filter.type`, checked before every request |
| `footfall/agent.py` | the agent: locate, survey, investigate (tool calls), write |
| `footfall/checker.py` | cuts anything not in the evidence ledger |
| `footfall/evidence.py` | the evidence ledger |
| `footfall/llm.py` | provider switch, one OpenAI-compatible chat call |
| `footfall/budget.py` | spend cap: prices, reservations, per-call checks, visitor limits |
| `footfall/store.py` | durable JSON store: a private GitHub repo, or a local folder |
| `footfall/replay.py` | the demo shops and their saved Qloo answers |
| `footfall/geocode.py` | address to coordinates (OpenStreetMap Nominatim) |
| `static/` | the web page (vanilla JS, Leaflet map) |
| `scripts/` | `probe_location.py` (live checks of Qloo's location parameters), `record_presets.py` (save the demo shops' Qloo answers) |
| `tests/` | tests on synthetic fixtures |

## Data

Test fixtures in `tests/fixtures/` are **made up**, shaped like the examples on docs.qloo.com. No real Qloo response is stored in this repository, as Qloo's terms require; the app caches responses privately on the server only, keeping just the fields it reads.

Map data © OpenStreetMap contributors. Geocoding by Nominatim, within its usage policy (one request a second, cached, one retry). If Nominatim is busy, Footfall asks Qloo's `/search` for the shop and checks the address matches.

## Licence

MIT, see [LICENSE](LICENSE).
