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
import hashlib
import json
import os
import queue
import re
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import numpy as np


def _find_repo_root(start):
    """Walk up from this script until we find the dir holding synth_farm/."""
    d = os.path.abspath(start)
    while True:
        if os.path.isfile(os.path.join(d, "synth_farm", "__init__.py")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


REPO_ROOT = _find_repo_root(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT is None:
    sys.stderr.write(
        "error: could not locate the synth-farm repo (no synth_farm/ package found\n"
        "above this script). cd to the folder that contains BOTH 'live_demo' and\n"
        "'synth_farm', then run:  .venv/bin/python live_demo/server.py\n"
    )
    sys.exit(1)
sys.path.insert(0, REPO_ROOT)

from synth_farm.personas import generate_personas, ARCHETYPES
from synth_farm.real_catalog import PROVIDER_APP_MAP
from synth_farm.collections import score_titles


# ----------------------------------------------------------------------------
# 30-genre taste taxonomy
#
# TMDb's native genres plus derived sub-genres (anime, k-drama, true-crime,
# superhero, sitcom, ...) detected from title/overview/language signals.
# Every one of the 30 has at least one title in the 117-title catalog.
# ----------------------------------------------------------------------------
GENRES30 = [
    "action", "adventure", "animation", "comedy", "crime", "documentary",
    "drama", "family", "fantasy", "horror", "music", "mystery",
    "romance", "sci-fi", "thriller", "war", "kids", "reality-tv",
    "superhero", "anime", "k-drama", "true-crime", "musical", "sports",
    "martial-arts", "romcom", "space", "supernatural", "sitcom", "dystopia",
]

_GENRE_PRETTY = {
    "sci-fi": "Sci-Fi", "k-drama": "K-Drama", "true-crime": "True Crime",
    "reality-tv": "Reality TV", "romcom": "Rom-Com",
}


def genre_pretty(g: str) -> str:
    return _GENRE_PRETTY.get(g, g.replace("-", " ").title())


_TMDB_BASE = {
    28: "action", 12: "adventure", 16: "animation", 35: "comedy",
    80: "crime", 99: "documentary", 18: "drama", 10751: "family",
    14: "fantasy", 27: "horror", 10402: "music",
    9648: "mystery", 10749: "romance", 878: "sci-fi", 53: "thriller",
    10752: "war", 10759: "action", 10762: "kids", 10764: "reality-tv",
    10765: "sci-fi", 10766: "drama", 10768: "war", 10770: "drama",
}


def _derived_tags(entry: dict, gids: list) -> list:
    t = f"{entry.get('title', '')} {entry.get('overview', '')}".lower()
    lang = entry.get("original_language")
    tags = []

    def has(pat: str) -> bool:
        return re.search(pat, t) is not None

    if 28 in gids and has(r"super|batman|spider|avenger|wonder|thor|iron man|hulk|"
                          r"captain america|marvel|x-men|superman|aquaman|shazam|"
                          r"panther|deadpool|justice league|guardians|fantastic four|daredevil"):
        tags.append("superhero")
    if 16 in gids and lang == "ja":
        tags.append("anime")
    if 18 in gids and lang == "ko":
        tags.append("k-drama")
    if (80 in gids or 99 in gids) and has(r"murder|killer|heist|cartel|prison|"
                                          r"missing|disappear|trial|detective|serial|mafia|"
                                          r"drug|fraud|scam|kidnap|true crime|cold case"):
        tags.append("true-crime")
    if 10402 in gids or (has(r"\bmusical\b|broadway") and (18 in gids or 35 in gids)):
        tags.append("musical")
    if has(r"football|soccer|basketball|olympic|formula|racing|golf|tennis|"
           r"\bsport\b|athlete|championship|wwe|ufc|boxing|world cup|marathon|nfl|nba|surfing"):
        tags.append("sports")
    if 10751 in gids and has(r"paw patrol|peppa|bluey|junior|children|kids\b|"
                             r"cocomelon|minions|toy story"):
        tags.append("kids")
    if has(r"kung fu|martial arts|samurai|ninja|karate|wushu|muay thai"):
        tags.append("martial-arts")
    if 35 in gids and 10749 in gids:
        tags.append("romcom")
    if 878 in gids and has(r"\bspace\b|mars|alien|galaxy|astronaut|interstellar"):
        tags.append("space")
    if 27 in gids and has(r"zombie|vampire|ghost|haunted|possess|demon|slasher"):
        tags.append("supernatural")
    if 35 in gids and entry.get("media_type") == "tv" and not has(r"stand-up|standup|comedy special"):
        tags.append("sitcom")
    if has(r"dystopia|post-apocalyptic|apocalypse\b|end of the world"):
        tags.append("dystopia")
    return tags


def _load_catalog_30(repo_root: str) -> SimpleNamespace:
    """Load the raw TMDb fixture and tag every title in the 30-genre space."""
    path = os.path.join(repo_root, "data", "catalog_tmdb.json")
    entries = json.load(open(path))["items"]
    items = []
    for e in entries:
        gids = e.get("genre_ids", [])
        tags: list[str] = []
        for gid in gids:
            b = _TMDB_BASE.get(gid)
            if b and b not in tags:
                tags.append(b)
        if 10759 in gids and "adventure" not in tags:
            tags.append("adventure")
        if 10765 in gids and "fantasy" not in tags:
            tags.append("fantasy")
        for d in _derived_tags(e, gids):
            if d not in tags:
                tags.append(d)
        if not tags:
            tags = ["drama"]
        vec = np.zeros(len(GENRES30))
        for t in tags:
            vec[GENRES30.index(t)] = 1.0 / len(tags)
        providers: list[str] = []
        for pname in e.get("providers_flatrate", []):
            app = PROVIDER_APP_MAP.get(pname)
            if app and app not in providers:
                providers.append(app)
        media = e.get("media_type", "")
        tid = int(e["tmdb_id"])
        release = e.get("release_date") or ""
        items.append(SimpleNamespace(
            item_id=f"tmdb-{media}-{tid}",
            title=str(e.get("title") or f"Untitled {tid}"),
            primary_genre=tags[0],
            genre_tags=tuple(tags),
            genre_vector=vec,
            popularity=float(e.get("popularity") or 0.0),
            vote_average=float(e.get("vote_average") or 0.0),
            year=int(release[:4]) if len(release) >= 4 and release[:4].isdigit() else 0,
            release_date=str(e.get("release_date") or ""),
            media_type=media,
            poster_path=str(e.get("poster_path") or ""),
            providers=tuple(providers),
        ))
    return SimpleNamespace(items=items)


def _profile_for(persona_id: str) -> dict:
    """Stable synthetic identity attributes, derived from the persona id."""
    h = int(hashlib.md5(persona_id.encode()).hexdigest(), 16)
    return {
        "age_band": ["18–24", "25–34", "35–44", "45–54", "55+"][h % 5],
        "region": ["West", "Southwest", "Midwest", "Northeast", "Southeast"][(h >> 3) % 5],
        "primary_device": ["Roku", "Fire TV", "Apple TV", "Smart TV", "Chromecast"][(h >> 6) % 5],
        "household_size": ["1", "2", "3", "4", "5+"][(h >> 9) % 5],
    }


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

        self.genres = list(GENRES30)
        self.gidx = {g: i for i, g in enumerate(self.genres)}
        self.personas = generate_personas(n_agents, self.genres, seed=seed)
        self.catalog = _load_catalog_30(REPO_ROOT)
        self.by_id = {it.item_id: it for it in self.catalog.items}
        self.by_tag: dict[str, list] = {}
        for it in self.catalog.items:
            for t in it.genre_tags:
                self.by_tag.setdefault(t, []).append(it)
        self.last_update: dict[str, dict] = {}

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
                items = self.by_tag.get(g) or self.catalog.items
                item = items[int(rng.integers(len(items)))]
                before = float(self.tastes[pi][self.gidx[g]])
                self.tastes[pi] = ((1 - alpha) * self.tastes[pi]
                                   + alpha * item.genre_vector)
                after = float(self.tastes[pi][self.gidx[g]])
                ev = {"day": self.day + 1, "persona_id": p.persona_id,
                      "archetype": p.archetype, "type": kind,
                      "item_id": item.item_id, "title": item.title, "genre": g}
                if kind == "search":
                    ev["query"] = genre_pretty(g)
                evs = self.agent_events[p.persona_id]
                evs.append(ev)
                if len(evs) > 400:
                    del evs[:len(evs) - 400]
                self.last_update[p.persona_id] = {
                    "genre": g, "kind": kind, "title": item.title,
                    "before": round(before, 4), "after": round(after, 4),
                    "day": self.day + 1}
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
            p = next((pp for pp in self.personas if pp.persona_id == persona_id), None)
            item = self.by_id.get(item_id)
            if p is None or item is None:
                raise KeyError("unknown agent or title")
            pi = self.personas.index(p)
            evs = self.agent_events[persona_id]
            for kind in ("impression", "click", "play"):
                ev = {"day": self.day, "persona_id": persona_id,
                      "archetype": p.archetype, "type": kind,
                      "item_id": item_id, "title": item.title,
                      "genre": item.primary_genre, "live": True}
                evs.append(ev)
                self.events.append(ev)
            if len(evs) > 400:
                del evs[:len(evs) - 400]
            self.watched[persona_id].add(item_id)
            g = self.gidx[item.primary_genre]
            before = float(self.tastes[pi][g])
            self.tastes[pi] = (0.85 * self.tastes[pi]
                               + 0.15 * item.genre_vector)
            after = float(self.tastes[pi][g])
            self.last_update[persona_id] = {
                "genre": item.primary_genre, "kind": "play",
                "title": item.title, "before": round(before, 4),
                "after": round(after, 4), "day": self.day}
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
        counts = {k: len([e for e in evs if e["type"] == k])
                  for k in ("impression", "click", "play", "search")}
        return {
            "id": p.persona_id,
            "archetype": p.archetype,
            "archetype_pretty": p.archetype.replace("_", " ").title(),
            "profile": _profile_for(p.persona_id),
            "taste": {g: round(float(w), 4) for g, w in zip(self.genres, taste)},
            "top_genre": self.genres[int(taste.argmax())],
            "sessions_per_week": p.sessions_per_week,
            "search_propensity": round(p.search_propensity, 3),
            "clickiness": round(p.clickiness, 3),
            "completion_propensity": round(p.completion_propensity, 3),
            "mean_units": round(float(p.mean_units), 2),
            "subscribed_apps": list(p.subscribed_apps),
            "n_events": len(evs),
            "n_plays": counts["play"],
            "event_counts": counts,
            "last_taste_update": self.last_update.get(p.persona_id),
            "recent_plays": [{"title": e["title"], "genre": e["genre"],
                              "day": e["day"]} for e in plays],
            "recent_events": [{"type": e["type"], "title": e["title"],
                               "genre": e["genre"], "day": e["day"],
                               "query": e.get("query")}
                              for e in evs[-14:][::-1]],
            "cluster": int(self.labels[pi]),
            "home_screen": self.home_screen(p),
        }

    def _item_json(self, item, score=None):
        d = {"id": item.item_id, "title": item.title,
             "genre": genre_pretty(item.primary_genre),
             "poster": (f"https://image.tmdb.org/t/p/w342{item.poster_path}"
                        if item.poster_path else ""),
             "providers": list(item.providers)}
        if score is not None:
            d["score"] = round(score, 3)
        return d

    def _cluster_favorites(self, pi: int, unseen: set, n: int) -> list:
        """Titles most played by the agent's own cluster members (live collab filtering)."""
        lab = int(self.labels[pi])
        counts: dict[str, int] = {}
        for qi, q in enumerate(self.personas):
            if int(self.labels[qi]) != lab:
                continue
            for e in self.agent_events[q.persona_id]:
                if e["type"] == "play" and e["item_id"] in unseen:
                    counts[e["item_id"]] = counts.get(e["item_id"], 0) + 1
        ranked_ids = sorted(counts, key=lambda i: (-counts[i], i))
        return [self.by_id[i] for i in ranked_ids[:n] if i in self.by_id]

    def home_screen(self, p, n: int = 20) -> dict:
        pi = self.personas.index(p)
        taste = self.tastes[pi]
        seen = self.watched[p.persona_id]
        ranked = [(it, s) for it, s in score_titles(taste, self.catalog)
                  if it.item_id not in seen]
        ranked_items = [it for it, _ in ranked]
        unseen = {it.item_id for it in ranked_items}

        def fill(items: list, want: int = n, backup: list | None = None) -> list:
            """Top up a short rail so every row is full (default backup: taste-ranked)."""
            src = ranked_items if backup is None else backup
            have = {it.item_id for it in items}
            out = list(items)
            for it in src:
                if len(out) >= want:
                    break
                if it.item_id not in have:
                    out.append(it)
                    have.add(it.item_id)
            return out[:want]

        def pool(genre: str) -> list:
            return [it for it in self.by_tag.get(genre, []) if it.item_id in unseen]

        def j(items: list) -> list:
            return [self._item_json(it) for it in items]

        order = np.argsort(taste)[::-1]
        g1, g2 = self.genres[order[0]], self.genres[order[1]]
        evs = self.agent_events[p.persona_id]
        last_play = next((e for e in reversed(evs) if e["type"] == "play"), None)

        rails: list[tuple[str, str, list]] = []
        rails.append(("Personalized for you",
                      "ranked live against this agent's 30-genre taste vector",
                      [self._item_json(it, s) for it, s in ranked[:n]]))
        rails.append((f"Because of your interest in {genre_pretty(g1)}", "",
                      j(fill(pool(g1)))))
        trending_idx = np.argsort(self.genre_plays)[::-1][:3]
        trending_genres = {self.genres[i] for i in trending_idx if self.genre_plays[i] > 0}
        tr_pool = [it for it in ranked_items if it.primary_genre in trending_genres]
        rails.append(("Trending now", "what the whole simulation is watching today",
                      j(fill(tr_pool))))
        new_pool = sorted(
            (it for it in self.catalog.items
             if it.item_id in unseen and it.release_date >= "2026-08-01"),
            key=lambda it: -it.popularity)
        rails.append(("New this month", "released in the last 60 days",
                      j(fill(new_pool[:n]))))
        if last_play and last_play["item_id"] in self.by_id:
            bw_pool = [it for it in pool(last_play["genre"])
                       if it.item_id != last_play["item_id"]]
            rails.append((f"Because you watched {last_play['title']}", "",
                          j(fill(bw_pool))))
        else:
            rails.append((f"More {genre_pretty(g2)}", "your second-favorite genre",
                          j(fill(pool(g2)))))
        by_pop = sorted((it for it in self.catalog.items if it.item_id in unseen),
                        key=lambda it: -it.popularity)
        rails.append(("Popular right now", "highest TMDb popularity today",
                      j(fill(by_pop[:n]))))
        top5 = {self.genres[i] for i in order[:5]}
        detour_pool = sorted(
            (it for it in self.catalog.items
             if it.item_id in unseen and it.primary_genre not in top5),
            key=lambda it: (-it.vote_average, -it.popularity))
        rails.append(("Worth the detour",
                      "top-rated picks outside your usual genres — variety beats fatigue",
                      j(fill(detour_pool[:n], backup=detour_pool))))
        cont: list = []
        cont_ids: set = set()
        for e in reversed(evs):
            if e["type"] == "play" and e["item_id"] in self.by_id:
                it = self.by_id[e["item_id"]]
                if it.item_id not in cont_ids:
                    cont.append(it)
                    cont_ids.add(it.item_id)
            if len(cont) >= n:
                break
        rails.append(("Continue watching", "", j(fill(cont))))
        by_vote = sorted((it for it in self.catalog.items if it.item_id in unseen),
                         key=lambda it: (-it.vote_average, it.item_id))
        rails.append(("Critics' picks", "highest rated of all time",
                      j(fill(by_vote[:n]))))
        fav = self._cluster_favorites(pi, unseen, n)
        rails.append(("Viewers like you watch",
                      f"most played in cluster {int(self.labels[pi])} today",
                      j(fill(fav))))
        hidden_pool = by_pop[len(by_pop) // 2:]
        gems = sorted(hidden_pool,
                      key=lambda it: -float(np.dot(taste, it.genre_vector)))
        rails.append(("Hidden gems for you", "high taste match, low popularity",
                      j(fill(gems[:n]))))
        rails.append((f"Marathon weekend: {genre_pretty(g2)}", "",
                      j(fill(pool(g2)))))
        app = p.subscribed_apps[0] if p.subscribed_apps else None
        if app:
            app_pool = sorted(
                (it for it in self.catalog.items
                 if it.item_id in unseen and app in it.providers),
                key=lambda it: -float(np.dot(taste, it.genre_vector)))
            rails.append((f"On {app}", f"top picks from this agent's {app} subscription",
                          j(fill(app_pool))))
        return {"rails": [{"title": t, "why": w, "items": items}
                          for t, w, items in rails]}

    def search(self, q: str, persona_id: str | None = None, n: int = 12) -> list[dict]:
        ql = q.lower().strip()
        taste = None
        if persona_id:
            p = next((pp for pp in self.personas if pp.persona_id == persona_id), None)
            if p is not None:
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

    def _error(self, code: int, message: str):
        return self._json({"error": message}, code=code)

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
                        "genres": [{"key": g, "name": genre_pretty(g),
                                    "titles": len(sim.by_tag.get(g, []))}
                                   for g in sim.genres],
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
                    p = next((pp for pp in sim.personas if pp.persona_id == pid), None)
                    if p is None:
                        return self._error(404, "no such agent")
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
                try:
                    result = sim.click(data["agent_id"], data["item_id"])
                except KeyError:
                    return self._error(404, "unknown agent or title")
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
