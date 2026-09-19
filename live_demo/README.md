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

- **Clusters tab** — six taste clusters recomputed from the agents' actual
  taste vectors every simulated day. Sizes shift as the sim runs.
- **Agents tab** — every agent's live data model: taste vector, behavior
  params, subscriptions, growing event history.
- **Home Screen tab** — rails ranked live against the selected agent's
  *current* taste vector (`collections.score_titles`). Clicking a tile POSTs
  to the sim: impression → click → play events are recorded, the taste vector
  updates, rails re-rank. Search box queries the 117-title real catalog,
  ranked by the agent's taste.
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
