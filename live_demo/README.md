# Synth Farm — LIVE demo

The real simulation, running in real time. The Python agents genuinely execute
here: every tick is one simulated day of watching and searching, taste vectors
update, clusters form. The web page is a **live window** into the running
process — not a recording.

## Run it

```bash
cd synth-farm
git pull
.venv/bin/python live_demo/server.py
```

Then open **http://localhost:8000** and press **Play**.

Options:

```bash
.venv/bin/python live_demo/server.py --port 8000 --tick 1.5 --agents 240 --seed 7
```

| Flag | Default | Meaning |
|------|---------|---------|
| `--port` | 8000 | HTTP port |
| `--tick` | 1.5 | seconds per simulated day (the demo clock is accelerated) |
| `--agents` | 240 | number of live agents |
| `--seed` | 7 | deterministic seed |

No new dependencies — stdlib only (`http.server`, `threading`) plus numpy,
which is already in `requirements.txt`. Works on Python 3.9+.

## What's live

- **30 taste dimensions** — the complete Gracenote genre taxonomy (29
  dimensions) plus **Bollywood** as a 30th, explicitly non-Gracenote axis.
  Hindi-language titles get 50% of their vector on Bollywood, 50% spread over
  their Gracenote genres. The 500-title TMDb catalog holds ~190 Bollywood
  titles; agent **#7** (persona-00006) is a dedicated Bollywood pivot whose
  top taste dimension is Bollywood.
- **13 rails, each with its own ordering** — Continue watching (recency),
  Personalized (taste match), genre row (top-rated in your #1 genre),
  Trending (today's genre heat), New this month (newest first),
  Because-you-watched (similarity to that title), Popular (popularity),
  Worth the detour (taste match outside your top-5 genres), Critics' picks
  (rating), Viewers like you (cluster consumption), Hidden gems (best of the
  low-popularity), Marathon weekend, and On {app}. Titles may repeat across
  rows, but no two rows share the same order.
- **🎭 Master Agent** — a plain-English command box (Master Agent tab)
  that pivots cohorts of agents toward any genre: *"NFL is starting, let's
  pivot some users to watch football for 7 days"* targets 25% of agents with
  a Sports campaign for 7 days. Supports `few` (10%), `half` (50%),
  `most` (60%), `all`/`everyone` (100%), explicit percentages, `cluster N`,
  and `for N days`. Campaigns fade slowly and steadily — full strength on day
  one, linearly down to a whisper on the last day — then expire automatically;
  `stop campaigns` clears them. API: `POST /api/direct {"text": ...}`;
  campaigns and the director log appear in `GET /api/status` and SSE
  `directed` events.
- **Two hand-built baseball lovers** — `persona-seamhead` (Baseball Purist:
  52% Sports with a documentary bench, watches everything to completion) and
  `persona-socialfan` (Social Fan: 37% Sports plus comedy/reality, searches
  and samples). Same favorite sport, different humans. Their hand-tuned
  tastes are written into the live taste array, not just the persona record.
- **MLB shelf** — 16 baseball films and documentaries (Moneyball, Field of
  Dreams, 42, Ken Burns: Baseball, …) appended to the catalog at load time
  with negative TMDb ids, so the fixture file stays pristine.
- **Where-to-watch on every tile** — real US flat-rate providers where TMDb
  has them; a deterministic mock (Netflix, HBO Max, Prime Video, …) fills the
  rest so no tile is ever blank. Mock only, not real-world accurate.
- **Why-this-was-recommended explainer** — pause the sim and click any tile
  to get a bottom sheet listing the concrete signals behind the
  recommendation: taste-vector contribution, prior watches in matching
  genres (with title and day), searches (with query and day), lookalike
  viewers, collection membership, active Master Agent campaigns, TMDb rating,
  release month, streaming providers. API: `GET /api/explain?agent=&item=`.
- **Clickable cluster dots** — on the Journey tab every dot is a live agent:
  click one for its number, archetype, cluster, top genre, play/event counts,
  top-5 taste weights, and recent plays — with a link to the full agent
  data-model page.
- **Gracenote genre taste model** — the complete industry metadata taxonomy
  (Gracenote ScreenPlay): all 29 Gracenote video genres are modeled as taste
  dimensions, exactly as Gracenote provides them. Six have no titles in our
  catalog (Ambient, Erotica, Game-Show, History, News, Western) — they stay
  in the taxonomy at ~zero weight rather than cut.
- **Clusters tab** — 20 taste clusters recomputed from the agents' actual
  taste vectors every simulated day. Sizes shift as the sim runs.
- **Agents tab** — click any agent to open its **data-model page**: identity
  captured at signup, behavioral DNA inferred from usage, the live
  Gracenote-genre taste vector, a "how data is collected" pipeline
  (observe → learn → cluster → rank), the event taxonomy with live counts,
  and a live event stream.
- **Home Screen tab** — 13 collections of 20 titles, ranked live against the
  selected agent's *current* Gracenote-genre taste vector
  (`collections.score_titles`). "Continue watching" leads, then taste rails,
  trending / new / popular, a deliberate "Worth the
  detour" rail of top-rated picks outside the agent's usual genres (variety
  beats fatigue), and lookalike picks from the agent's cluster.
  Rails re-rank automatically as simulated days tick, and the rows themselves
  move up and down with the day's activity — "Because you watched" jumps up
  right after a viewing, "Trending now" rises when the sim's watches
  concentrate, "Viewers like you watch" climbs when the agent's cluster is
  active, "Marathon weekend" surges on binge days. "Continue watching" stays
  pinned at the top. The DAY counter is
  clickable — it pauses/resumes the simulation, freezing the day count.
  Clicking a tile POSTs to the sim: impression → click → play events are
  recorded, the taste vector updates, rails re-rank. Search box queries the
  117-title real catalog, ranked by the agent's taste.
- **Journey tab** — 240 dots walk from a day-0 cold-start crowd into emergent
  clusters as simulated days tick. Trails show each agent's path.
- **Event ticker** — server-sent events stream live watches/searches.

## How it works

`server.py` runs `LiveSim` in a background thread: each tick, every agent
watches a few titles (taste-weighted + social trending + search discovery)
and its taste vector nudges toward what it consumed — the same behavior loop
as `scripts/sim_evolution.py`, but executing live. Warm-started k-means tracks
emergent clusters; a sign-stabilized PCA projects tastes to 2D for the
Journey view.

HTTP API (all JSON):

- `GET /api/status` — day, running, cluster sizes, event totals
- `GET /api/agents` — agent roster
- `GET /api/agent?id=` — full live data model + home screen rails
- `GET /api/journey` — 2D paths + cluster labels
- `GET /api/search?q=&agent=` — catalog search ranked by agent taste
- `GET /api/explain?agent=&item=` — why this title was recommended, with evidence
- `POST /api/direct {"text": ...}` — Master Agent: plain-English cohort pivots
- `POST /api/click` `{"agent_id","item_id"}` — record a watch, update taste
- `POST /api/control` `{"action":"play|pause|step|reset|speed", ...}`
- `GET /api/stream` — SSE live event feed

## Demo script (2 minutes)

1. Open the page, press **Play**. Watch the ticker: agents start watching.
2. **Journey tab** — one crowd on day 0; by day ~20 they've walked into
   genre streams.
3. **Home Screen tab** — pick an agent, note their taste bars, click a tile.
   The bars move *now*, the rails re-rank *now*.
4. Closer: *"Every click you just saw went into the running Python process —
   no precomputed data, no slides."*
