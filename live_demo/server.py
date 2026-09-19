#!/usr/bin/env python3
"""Synth Farm LIVE demo server — the real simulation, running in real time.

The Python agents genuinely execute here: every tick is one simulated day of
watching/searching, tastes update, clusters form. The web page is a live
window into this process (polling + SSE), not a recording.

    python demo_server.py [--port 8000] [--tick 1.5] [--agents 240] [--seed 7]

Then open http://localhost:8000 and press Play.
"""
from __future__ import annotations

import argparse
import json
import queue
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

from synth_farm.config import Config
from synth_farm.personas import generate_personas, ARCHETYPES
from synth_farm.real_catalog import load_real_catalog
from synth_farm.collections import score_titles


# ----------------------------------------------------------------------------
# Live simulation
# ----------------------------------------------------------------------------
class LiveSim:
    def __init__(self, n_agents: int = 240, seed: int = 7, tick_seconds: float = 1.5):
        self.n_agents = n_agents
        self.seed = seed
        self.tick_seconds = tick_seconds
        self.rng = np.random.default_rng(seed)
        self.lock = threading.RLock()

        config = Config.from_env()
        self.genres = list(config.genres)
        self.gidx = {g: i for i, g in enumerate(self.genres)}
        self.personas = generate_personas(n_agents, self.genres, seed=seed)
        self.catalog = load_real_catalog()
        self.by_id = {it.item_id: it for it in self.catalog.items}
        self.by_genre: dict[str, list] = {}
        for it in self.catalog.items:
            self.by_genre.setdefault(it.primary_genre, []).append(it)

        # Cold start: near-uniform tastes. Behavior params stay distinct.
        self.tastes = self.rng.dirichlet(np.full(len(self.genres), 5.0), size=n_agents)
        self.day = 0
        self.paths = [self.tastes.copy()]
        self.events: list[dict] = []          # global recent event feed
        self.agent_events: dict[str, list[dict]] = {p.persona_id: [] for p in self.personas}
        self.watched: dict[str, set[str]] = {p.persona_id: set() for p in self.personas}
        self.genre_plays = np.zeros(len(self.genres))  # live trending counts

        self.cluster_trend = {a.name: np.ones(len(self.genres)) / len(self.genres)
                              for a in ARCHETYPES}
        self.global_trend = np.ones(len(self.genres)) / len(self.genres)

        # Emergent clusters via warm-started k-means on tastes.
        self.k = 6
        self.centroids = self.tastes[self.rng.choice(n_agents, self.k, replace=False)]
        self.labels = np.zeros(n_agents, dtype=int)
        self._recluster()
        self._refit_projection()
        self._recalc_paths_2d()

        self.running = False
        self.subscribers: list[queue.Queue] = []
        self._stop = threading.Event()

    # -- clustering / projection ------------------------------------------------
    def _recluster(self):
        for _ in range(25):
            d2 = ((self.tastes[:, None, :] - self.centroids[None, :, :]) ** 2).sum(-1)
            lab = d2.argmin(1)
            new = np.array([self.tastes[lab == k].mean(0) if (lab == k).any()
                            else self.centroids[k] for k in range(self.k)])
            if np.allclose(new, self.centroids):
                break
            self.centroids = new
        self.labels = d2.argmin(1)

    def _refit_projection(self):
        flat = np.stack(self.paths).reshape(-1, len(self.genres))
        self._pca_mean = flat.mean(axis=0)
        _, _, vt = np.linalg.svd(flat - self._pca_mean, full_matrices=False)
        new_axes = vt[:2].T
        if hasattr(self, "_pca_axes"):
            # stabilize orientation: flip signs to match previous projection
            for j in range(2):
                if float(new_axes[:, j] @ self._pca_axes[:, j]) < 0:
                    new_axes[:, j] *= -1
        self._pca_axes = new_axes

    def _project(self, t: np.ndarray) -> np.ndarray:
        return (t - self._pca_mean) @ self._pca_axes

    def _recalc_paths_2d(self):
        stacked = np.stack(self.paths)                       # (D+1, N, G)
        p2 = np.stack([self._project(stacked[d]) for d in range(stacked.shape[0])])
        mn, mx = p2.min(axis=(0, 1)), p2.max(axis=(0, 1))
        span = np.maximum(mx - mn, 1e-9)
        self.paths_2d = (p2 - mn) / span                     # (D+1, N, 2) in [0,1]^2

    def cluster_info(self):
        info = []
        for k in range(self.k):
            top = self.genres[int(self.centroids[k].argmax())]
            c2 = self._project(self.centroids[k][None, :])[0]
            info.append({
                "id": k,
                "name": f"{top.title()} cluster",
                "top_genre": top,
                "size": int((self.labels == k).sum()),
            })
        return info

    # -- one simulated day ------------------------------------------------------
    def tick(self):
        rng = self.rng
        day_counts = {a.name: np.zeros(len(self.genres)) for a in ARCHETYPES}
        day_total = np.zeros(len(self.genres))
        feed = []
        for pi, p in enumerate(self.personas):
            n_watch = int(rng.poisson(4.0) + 1)
            for _ in range(n_watch):
                mix = (0.6 * self.tastes[pi] + 0.25 * self.cluster_trend[p.archetype]
                       + 0.15 * self.global_trend)
                mix = mix / mix.sum()
                if rng.random() < p.search_propensity:
                    g = self.genres[int(rng.integers(len(self.genres)))]
                    alpha, kind = 0.05, "search"
                else:
                    g = self.genres[int(rng.choice(len(self.genres), p=mix))]
                    alpha, kind = 0.12, "play"
                self.tastes[pi] = ((1 - alpha) * self.tastes[pi]
                                   + alpha * np.eye(len(self.genres))[self.gidx[g]])
                items = self.by_genre.get(g) or self.catalog.items
                item = items[int(rng.integers(len(items)))]
                ev = {"day": self.day + 1, "persona_id": p.persona_id,
                      "archetype": p.archetype, "type": kind,
                      "item_id": item.item_id, "title": item.title, "genre": g}
                self.agent_events[p.persona_id].append(ev)
                self.watched[p.persona_id].add(item.item_id)
                day_counts[p.archetype][self.gidx[g]] += 1
                day_total[self.gidx[g]] += 1
                if len(feed) < 6 and rng.random() < 0.3:
                    feed.append(ev)
        for a in ARCHETYPES:
            tot = day_counts[a.name].sum()
            if tot > 0:
                self.cluster_trend[a.name] = (0.5 * self.cluster_trend[a.name]
                                              + 0.5 * day_counts[a.name] / tot)
        tot = day_total.sum()
        if tot > 0:
            self.genre_plays += day_total
            self.global_trend = 0.5 * self.global_trend + 0.5 * day_total / tot

        self.day += 1
        self.paths.append(self.tastes.copy())
        if len(self.paths) > 61:
            self.paths.pop(0)
        self._recluster()
        self._refit_projection()
        self._recalc_paths_2d()
        self.events.extend(feed)
        self.events = self.events[-200:]
        for q in list(self.subscribers):
            try:
                q.put_nowait({"kind": "tick", "day": self.day, "feed": feed})
            except queue.Full:
                pass

    # -- user interaction -------------------------------------------------------
    def click(self, persona_id: str, item_id: str) -> dict:
        with self.lock:
            p = next(pp for pp in self.personas if pp.persona_id == persona_id)
            item = self.by_id[item_id]
            pi = self.personas.index(p)
            for kind in ("impression", "click", "play"):
                ev = {"day": self.day, "persona_id": persona_id,
                      "archetype": p.archetype, "type": kind,
                      "item_id": item_id, "title": item.title,
                      "genre": item.primary_genre, "live": True}
                self.agent_events[persona_id].append(ev)
                self.events.append(ev)
            self.watched[persona_id].add(item_id)
            g = self.gidx[item.primary_genre]
            self.tastes[pi] = (0.85 * self.tastes[pi]
                               + 0.15 * np.eye(len(self.genres))[g])
            self._recluster()
            for q in list(self.subscribers):
                try:
                    q.put_nowait({"kind": "click", "persona_id": persona_id,
                                  "title": item.title, "genre": item.primary_genre})
                except queue.Full:
                    pass
            return self.agent_payload(p)

    # -- read APIs --------------------------------------------------------------
    def agent_payload(self, p) -> dict:
        pi = self.personas.index(p)
        taste = self.tastes[pi]
        evs = self.agent_events[p.persona_id]
        plays = [e for e in evs if e["type"] == "play"][-8:][::-1]
        return {
            "id": p.persona_id,
            "archetype": p.archetype,
            "archetype_pretty": p.archetype.replace("_", " ").title(),
            "taste": {g: round(float(w), 4) for g, w in zip(self.genres, taste)},
            "top_genre": self.genres[int(taste.argmax())],
            "sessions_per_week": p.sessions_per_week,
            "search_propensity": round(p.search_propensity, 3),
            "clickiness": round(p.clickiness, 3),
            "completion_propensity": round(p.completion_propensity, 3),
            "subscribed_apps": list(p.subscribed_apps),
            "n_events": len(evs),
            "n_plays": len([e for e in evs if e["type"] == "play"]),
            "recent_plays": [{"title": e["title"], "genre": e["genre"],
                              "day": e["day"]} for e in plays],
            "cluster": int(self.labels[pi]),
            "home_screen": self.home_screen(p),
        }

    def _item_json(self, item, score=None):
        d = {"id": item.item_id, "title": item.title,
             "genre": item.primary_genre,
             "poster": (f"https://image.tmdb.org/t/p/w342{item.poster_path}"
                        if item.poster_path else ""),
             "providers": list(item.providers)}
        if score is not None:
            d["score"] = round(score, 3)
        return d

    def home_screen(self, p, n: int = 8) -> dict:
        pi = self.personas.index(p)
        taste = self.tastes[pi]
        seen = self.watched[p.persona_id]
        ranked = [(it, s) for it, s in score_titles(taste, self.catalog)
                  if it.item_id not in seen]
        top_genre = self.genres[int(taste.argmax())]
        evs = self.agent_events[p.persona_id]
        last_play = next((e for e in reversed(evs) if e["type"] == "play"), None)
        genre_items = [it for it in self.by_genre.get(top_genre, [])
                       if it.item_id not in seen]
        bw_items = []
        bw_title = None
        if last_play:
            bw_title = last_play["title"]
            bw_items = [it for it in self.by_genre.get(last_play["genre"], [])
                        if it.item_id not in seen and it.item_id != last_play["item_id"]]
        trending_idx = np.argsort(self.genre_plays)[::-1][:3]
        trending_genres = {self.genres[i] for i in trending_idx if self.genre_plays[i] > 0}
        trending = [it for it, _ in ranked if it.primary_genre in trending_genres][:n] \
            or [it for it, _ in ranked[:n]]
        continue_watching = []
        for e in reversed(evs):
            if e["type"] == "play" and e["item_id"] in self.by_id:
                if self.by_id[e["item_id"]] not in continue_watching:
                    continue_watching.append(self.by_id[e["item_id"]])
            if len(continue_watching) >= n:
                break
        return {
            "personalized": [self._item_json(it, s) for it, s in ranked[:n]],
            "interest": {"genre": top_genre,
                         "items": [self._item_json(it) for it in genre_items[:n]]},
            "because_watched": {"title": bw_title,
                                "items": [self._item_json(it) for it in bw_items[:n]]},
            "trending": [self._item_json(it) for it in trending],
            "continue": [self._item_json(it) for it in continue_watching],
        }

    def search(self, q: str, persona_id: str | None = None, n: int = 12) -> list[dict]:
        ql = q.lower().strip()
        taste = None
        if persona_id:
            p = next(pp for pp in self.personas if pp.persona_id == persona_id)
            taste = self.tastes[self.personas.index(p)]
        scored = []
        for it in self.catalog.items:
            hay = f"{it.title} {it.primary_genre}".lower()
            if ql and ql not in hay:
                continue
            s = float(np.dot(taste, it.genre_vector)) if taste is not None else 0.0
            # prefix / title matches rank first
            boost = 2.0 if it.title.lower().startswith(ql) else (1.0 if ql in it.title.lower() else 0.0)
            scored.append((boost, s, it))
        scored.sort(key=lambda t: (-t[0], -t[1], t[2].item_id))
        return [self._item_json(it, s) for _, s, it in scored[:n]]

    # -- main loop --------------------------------------------------------------
    def loop(self):
        while not self._stop.is_set():
            if self.running:
                t0 = time.time()
                with self.lock:
                    self.tick()
                dt = time.time() - t0
                time.sleep(max(0.05, self.tick_seconds - dt))
            else:
                time.sleep(0.1)

    def reset(self, seed=None):
        with self.lock:
            self.__init__(n_agents=self.n_agents, seed=seed or self.seed,
                          tick_seconds=self.tick_seconds)


# ----------------------------------------------------------------------------
# HTTP server (stdlib only)
# ----------------------------------------------------------------------------
STATIC_DIR = None  # set in main()

class Handler(BaseHTTPRequestHandler):
    sim: LiveSim

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _static(self, name, ctype):
        import os
        path = os.path.join(STATIC_DIR, name)
        if not os.path.exists(path):
            self.send_error(404)
            return
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        route, qs = url.path, urllib.parse.parse_qs(url.query)
        sim = self.sim
        try:
            if route == "/":
                return self._static("index.html", "text/html; charset=utf-8")
            if route == "/app.js":
                return self._static("app.js", "text/javascript; charset=utf-8")
            if route == "/styles.css":
                return self._static("styles.css", "text/css; charset=utf-8")
            if route == "/api/status":
                with sim.lock:
                    return self._json({
                        "day": sim.day, "running": sim.running,
                        "tick_seconds": sim.tick_seconds,
                        "n_agents": sim.n_agents,
                        "clusters": sim.cluster_info(),
                        "total_events": sum(len(v) for v in sim.agent_events.values()),
                    })
            if route == "/api/agents":
                with sim.lock:
                    agents = [{
                        "id": p.persona_id, "archetype": p.archetype,
                        "archetype_pretty": p.archetype.replace("_", " ").title(),
                        "top_genre": sim.genres[int(sim.tastes[sim.personas.index(p)].argmax())],
                        "n_plays": len([e for e in sim.agent_events[p.persona_id]
                                        if e["type"] == "play"]),
                        "cluster": int(sim.labels[sim.personas.index(p)]),
                    } for p in sim.personas]
                    return self._json({"agents": agents})
            if route == "/api/agent":
                pid = qs.get("id", [None])[0]
                with sim.lock:
                    p = next(pp for pp in sim.personas if pp.persona_id == pid)
                    return self._json(sim.agent_payload(p))
            if route == "/api/journey":
                with sim.lock:
                    paths = [[[round(float(x), 4), round(float(y), 4)]
                              for x, y in sim.paths_2d[:, i, :]]
                             for i in range(sim.n_agents)]
                    # labels per day: recompute cheaply from stored tastes? use current labels
                    # for the trail coloring we send final-day labels + per-day via paths only.
                    return self._json({
                        "day": sim.day,
                        "paths": paths,
                        "labels": [int(x) for x in sim.labels],
                        "clusters": sim.cluster_info(),
                    })
            if route == "/api/search":
                q = qs.get("q", [""])[0]
                pid = qs.get("agent", [None])[0]
                with sim.lock:
                    return self._json({"results": sim.search(q, pid)})
            if route == "/api/stream":
                return self._sse()
            self.send_error(404)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        sim = self.sim
        try:
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length) or b"{}")
            if url.path == "/api/click":
                result = sim.click(data["agent_id"], data["item_id"])
                return self._json(result)
            if url.path == "/api/control":
                action = data.get("action")
                with sim.lock:
                    if action == "play":
                        sim.running = True
                    elif action == "pause":
                        sim.running = False
                    elif action == "step":
                        sim.tick()
                    elif action == "reset":
                        sim.reset()
                    elif action == "speed":
                        sim.tick_seconds = float(data.get("tick_seconds", 1.5))
                return self._json({"ok": True, "day": sim.day,
                                   "running": sim.running})
            self.send_error(404)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _sse(self):
        sim = self.sim
        q: queue.Queue = queue.Queue(maxsize=50)
        with sim.lock:
            sim.subscribers.append(q)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(b": connected\n\n")
            while True:
                try:
                    msg = q.get(timeout=20)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                self.wfile.write(b"data: " + json.dumps(msg).encode() + b"\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with sim.lock:
                if q in sim.subscribers:
                    sim.subscribers.remove(q)


def main():
    global STATIC_DIR
    ap = argparse.ArgumentParser(description="Synth Farm live demo server")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--tick", type=float, default=1.5,
                    help="seconds per simulated day")
    ap.add_argument("--agents", type=int, default=240)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--static", default=None)
    args = ap.parse_args()

    import os
    STATIC_DIR = args.static or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                             "static")
    sim = LiveSim(n_agents=args.agents, seed=args.seed, tick_seconds=args.tick)
    Handler.sim = sim
    t = threading.Thread(target=sim.loop, daemon=True)
    t.start()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Synth Farm LIVE  →  http://localhost:{args.port}")
    print(f"  {args.agents} agents, 1 tick = 1 simulated day every {args.tick}s. Press Play.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping…")


if __name__ == "__main__":
    main()
