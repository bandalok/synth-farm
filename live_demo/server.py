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
import copy
import hashlib
import json
import os
import queue
import random
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

from synth_farm.personas import generate_personas, ARCHETYPES, Persona
from synth_farm.real_catalog import PROVIDER_APP_MAP
from synth_farm.collections import score_titles


# ----------------------------------------------------------------------------
# Gracenote genre taxonomy (video) — the complete list.
#
# Source: Gracenote ScreenPlay Catalog Access docs (devportal.gracenote.com) —
# the industry metadata taxonomy used across streaming/TV platforms.
# All 29 Gracenote video genres are modeled as taste dimensions, exactly as
# Gracenote provides them. Six have no titles in our TMDb catalog
# (Ambient, Erotica, Game-Show, History, News, Western) — they stay in the
# taxonomy at ~zero weight rather than being cut.
# "Bollywood" is a 30th, non-Gracenote dimension: it is how viewers actually
# browse (Hindi cinema cuts across Drama/Romance/Musical/Action), so it earns
# its own taste axis. Titles with original_language == "hi" get Bollywood
# weight in their genre vector.
# ----------------------------------------------------------------------------
GRACENOTE_GENRES = [
    "Action", "Adventure", "Ambient", "Animation", "Biography", "Comedy",
    "Crime", "Documentary", "Drama", "Erotica", "Family", "Fantasy",
    "Game-Show", "History", "Horror", "Music", "Musical", "Mystery",
    "News", "Reality", "Religious", "Romance", "Romantic Comedy",
    "Science Fiction", "SitCom", "Sports", "Thriller", "War", "Western",
    "Bollywood",
]


def genre_pretty(g: str) -> str:
    # Gracenote names are already display-ready.
    return g


# Master Agent command vocabulary: everyday words -> taste dimensions.
MASTER_GENRES = {
    "football": "Sports", "nfl": "Sports", "sports": "Sports",
    "baseball": "Sports", "mlb": "Sports",
    "soccer": "Sports", "basketball": "Sports", "cricket": "Sports",
    "ipl": "Sports", "tennis": "Sports", "f1": "Sports",
    "bollywood": "Bollywood", "hindi": "Bollywood", "desi": "Bollywood",
    "horror": "Horror", "scary": "Horror",
    "comedy": "Comedy", "funny": "Comedy", "sitcom": "SitCom",
    "drama": "Drama", "romance": "Romance", "romcom": "Romantic Comedy",
    "action": "Action", "thriller": "Thriller",
    "sci-fi": "Science Fiction", "scifi": "Science Fiction",
    "sci fi": "Science Fiction", "space": "Science Fiction",
    "documentary": "Documentary", "docuseries": "Documentary",
    "reality": "Reality", "crime": "Crime", "mystery": "Mystery",
    "fantasy": "Fantasy", "adventure": "Adventure",
    "animation": "Animation", "anime": "Animation",
    "family": "Family", "kids": "Family",
    "music": "Music", "musical": "Musical", "news": "News",
    "war": "War", "western": "Western", "history": "History",
    "religious": "Religious", "biography": "Biography",
}


_TMDB_BASE = {
    28: "Action", 12: "Adventure", 16: "Animation", 35: "Comedy",
    80: "Crime", 99: "Documentary", 18: "Drama", 10751: "Family",
    14: "Fantasy", 27: "Horror", 10402: "Music",
    9648: "Mystery", 10749: "Romance", 878: "Science Fiction",
    53: "Thriller", 10752: "War",
    10759: "Action", 10762: "Family", 10764: "Reality",
    10765: "Science Fiction", 10766: "Drama", 10768: "War", 10770: "Drama",
}


def _derived_tags(entry: dict, gids: list) -> list:
    """Gracenote genres not directly present in TMDb ids, detected from
    title/overview signals: Romantic Comedy, SitCom, Musical, Sports,
    Biography, Religious."""
    t = f"{entry.get('title', '')} {entry.get('overview', '')}".lower()
    tags = []

    def has(pat: str) -> bool:
        return re.search(pat, t) is not None

    if 35 in gids and 10749 in gids:
        tags.append("Romantic Comedy")
    if 35 in gids and entry.get("media_type") == "tv" and not has(r"stand-up|standup|comedy special"):
        tags.append("SitCom")
    if 10402 in gids or (has(r"\bmusical\b|broadway") and (18 in gids or 35 in gids)):
        tags.append("Musical")
    if has(r"football|soccer|basketball|olympic|formula|racing|golf|tennis|"
           r"\bsport\b|athlete|championship|wwe|ufc|boxing|world cup|marathon|nfl|nba|surfing"):
        tags.append("Sports")
    if has(r"based on a true story|biopic|life of |untold story"):
        tags.append("Biography")
    if has(r"\bfaith\b|jesus|christ|church|pastor|bible"):
        tags.append("Religious")
    return tags


# Synthetic MLB shelf appended to the catalog at load time: real baseball
# films and documentaries so the baseball agents and MLB campaigns have
# something to watch. (title, year, genre tags, popularity, vote_average)
# Sports leads every tuple: primary_genre is tags[0], so the tiles read
# as Sports (⚾) and watches reinforce the Sports taste dimension.
_MLB_INSERTS: tuple = (
    ("Field of Dreams", 1989, ("Sports", "Drama", "Fantasy"), 8.2, 7.5),
    ("Moneyball", 2011, ("Sports", "Drama"), 9.1, 7.6),
    ("42", 2013, ("Sports", "Drama", "History"), 7.4, 7.5),
    ("The Sandlot", 1993, ("Sports", "Family", "Comedy"), 8.0, 7.5),
    ("Bull Durham", 1988, ("Sports", "Comedy", "Romance"), 7.1, 7.0),
    ("A League of Their Own", 1992, ("Sports", "Comedy", "Drama"), 7.8, 7.3),
    ("Ken Burns: Baseball", 1994, ("Sports", "Documentary", "History"), 6.5, 8.6),
    ("The Battered Bastards of Baseball", 2014, ("Sports", "Documentary"), 6.2, 7.5),
    ("Fastball", 2016, ("Sports", "Documentary"), 5.4, 7.0),
    ("Screwball", 2018, ("Sports", "Documentary", "Comedy", "Crime"), 5.1, 6.8),
    ("Knuckleball!", 2012, ("Sports", "Documentary"), 4.8, 7.1),
    ("No No: A Dockumentary", 2014, ("Sports", "Documentary"), 4.9, 7.2),
    ("Trouble with the Curve", 2012, ("Sports", "Drama"), 6.8, 6.8),
    ("For Love of the Game", 1999, ("Sports", "Drama", "Romance"), 6.4, 6.6),
    ("61*", 2001, ("Sports", "Drama"), 5.9, 7.5),
    ("The Natural", 1984, ("Sports", "Drama"), 7.0, 7.2),
)

# Display names match the real provider mapping (HBO Max, Prime Video, ...).
_MOCK_PROVIDER_POOL = ("Netflix", "HBO Max", "Prime Video", "Hulu", "Disney+",
                       "Apple TV+", "Peacock", "Paramount+")


def _mock_providers(key: str) -> list[str]:
    """Deterministic mock 'where to watch' for titles with no provider data."""
    h = hashlib.md5(key.encode()).digest()
    a, b = h[0] % len(_MOCK_PROVIDER_POOL), h[1] % len(_MOCK_PROVIDER_POOL)
    if b == a:
        b = (b + 3) % len(_MOCK_PROVIDER_POOL)
    return [_MOCK_PROVIDER_POOL[a], _MOCK_PROVIDER_POOL[b]]


def _load_catalog_30(repo_root: str) -> SimpleNamespace:
    """Load the raw TMDb fixture and tag every title in the Gracenote genre space.

    A synthetic MLB shelf (16 baseball films/docs) is appended at load time so
    the baseball agents and MLB campaigns have something to watch. They carry
    negative tmdb_ids and never touch the fixture file.
    """
    path = os.path.join(repo_root, "data", "catalog_tmdb.json")
    entries = json.load(open(path))["items"]
    # Exact-500 catalog: drop the 16 weakest fixture entries — anything
    # posterless first, then the lowest-popularity non-Hindi titles (the
    # Hindi set is protected for the Bollywood dimension) — to make room
    # for the 16-title MLB shelf below.
    drop_ids: set = set()
    for e in entries:
        if not e.get("poster_path"):
            drop_ids.add(e["tmdb_id"])
    non_hi = sorted((e for e in entries if e["tmdb_id"] not in drop_ids
                     and e.get("original_language") != "hi"),
                    key=lambda e: e.get("popularity", 0))
    drop_ids.update(e["tmdb_id"] for e in non_hi[:16 - len(drop_ids)])
    entries = [e for e in entries if e["tmdb_id"] not in drop_ids]
    posters = json.load(open(os.path.join(repo_root, "data", "mlb_posters.json")))
    for i, (title, year, tags, pop, vote) in enumerate(_MLB_INSERTS):
        entries.append({
            "title": title, "media_type": "movie", "tmdb_id": -(1000 + i),
            "original_language": "en", "genre_ids": [],
            "overview": "", "poster_path": posters.get(title, ""),
            "release_date": f"{year}-01-01",
            "popularity": pop, "vote_average": vote, "vote_count": 0,
            "providers_flatrate": [], "synth_tags": list(tags),
        })
    assert len(entries) == 500, f"catalog must be exactly 500, got {len(entries)}"
    items = []
    for e in entries:
        tags: list[str] = []
        if "synth_tags" in e:
            tags = list(e["synth_tags"])
        else:
            gids = e.get("genre_ids", [])
            for gid in gids:
                b = _TMDB_BASE.get(gid)
                if b and b not in tags:
                    tags.append(b)
            if 10759 in gids and "Adventure" not in tags:
                tags.append("Adventure")
            if 10765 in gids and "Fantasy" not in tags:
                tags.append("Fantasy")
            for d in _derived_tags(e, gids):
                if d not in tags:
                    tags.append(d)
            if e.get("original_language") == "hi" and "Bollywood" not in tags:
                tags.append("Bollywood")
            if not tags:
                tags = ["Drama"]
        vec = np.zeros(len(GRACENOTE_GENRES))
        if "Bollywood" in tags:
            # Bollywood cuts across genres: half the weight on the Bollywood
            # axis, half spread over the title's Gracenote genres.
            others = [t for t in tags if t != "Bollywood"]
            bi = GRACENOTE_GENRES.index("Bollywood")
            if others:
                vec[bi] = 0.5
                for t in others:
                    vec[GRACENOTE_GENRES.index(t)] = 0.5 / len(others)
            else:
                vec[bi] = 1.0
        else:
            for t in tags:
                vec[GRACENOTE_GENRES.index(t)] = 1.0 / len(tags)
        providers: list[str] = []
        for pname in e.get("providers_flatrate", []):
            app = PROVIDER_APP_MAP.get(pname)
            if app and app not in providers:
                providers.append(app)
        media = e.get("media_type", "")
        tid = int(e["tmdb_id"])
        item_id = f"tmdb-{media}-{tid}"
        if not providers:
            # Mock "where to watch": every tile gets an answer even when the
            # real-world listing has none.
            providers = _mock_providers(item_id)
        release = e.get("release_date") or ""
        items.append(SimpleNamespace(
            item_id=item_id,
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


# Archetype names the live sim tracks, including the two hand-built baseball
# Hand-pinned taste anchors (they reuse the trend bookkeeping, not the
# sampled archetypes). Each pair shares a favorite genre but feels different.
ANCHOR_ARCHETYPES = ("baseball_purist", "social_fan",
                     "scifi_purist", "scifi_tourist")
_ALL_ARCHETYPE_NAMES = [a.name for a in ARCHETYPES] + list(ANCHOR_ARCHETYPES)

# persona_id -> pinned genre shown as a badge on the agent card.
ANCHOR_PINS = {
    "persona-seamhead": "Sports",
    "persona-socialfan": "Sports",
    "persona-voidwalker": "Science Fiction",
    "persona-nebula": "Science Fiction",
}


def _anchor_personas(genres: list[str]) -> list[Persona]:
    """Four hand-tuned taste anchors: two baseball lovers, two sci-fi lovers.

    - baseball_purist ("the seamhead"): lives for the game itself. Sports-heavy
      with a documentary/history bench for the Ken Burns stuff. Watches a ton,
      finishes everything, never searches.
    - social_fan: here for the hangout. Sports plus comedy/reality — the
      Friday-night crowd. Searches, samples, bails early.
    - scifi_purist ("the voidwalker"): lives in deep space. Science Fiction
      with a mystery/thriller bench. Binges whole series, finishes everything.
    - scifi_tourist: here for the spectacle. Sci-fi plus comedy/action — the
      blockbuster crowd. Clicks around, bails halfway.
    """
    n = len(genres)
    gi = {g: i for i, g in enumerate(genres)}

    def vec(spikes: dict[str, float], base: float = 0.006) -> np.ndarray:
        v = np.full(n, base)
        for g, w in spikes.items():
            v[gi[g]] = w
        return v / v.sum()

    return [
        Persona(
            persona_id="persona-seamhead",
            archetype="baseball_purist",
            taste=vec({"Sports": 0.50, "Documentary": 0.14, "Drama": 0.08,
                       "History": 0.05, "Biography": 0.04}),
            sessions_per_week=9,
            search_propensity=0.12,
            clickiness=1.2,
            completion_propensity=0.92,
            mean_units=3.0,
            genres=tuple(genres),
        ),
        Persona(
            persona_id="persona-socialfan",
            archetype="social_fan",
            taste=vec({"Sports": 0.30, "Comedy": 0.14, "Reality": 0.10,
                       "Drama": 0.08, "Romance": 0.05}),
            sessions_per_week=4,
            search_propensity=0.45,
            clickiness=1.6,
            completion_propensity=0.45,
            mean_units=1.8,
            genres=tuple(genres),
        ),
        Persona(
            persona_id="persona-voidwalker",
            archetype="scifi_purist",
            taste=vec({"Science Fiction": 0.52, "Mystery": 0.12,
                       "Thriller": 0.08, "Documentary": 0.05,
                       "Drama": 0.04}),
            sessions_per_week=10,
            search_propensity=0.10,
            clickiness=1.1,
            completion_propensity=0.95,
            mean_units=3.4,
            genres=tuple(genres),
        ),
        Persona(
            persona_id="persona-nebula",
            archetype="scifi_tourist",
            taste=vec({"Science Fiction": 0.28, "Comedy": 0.13,
                       "Action": 0.11, "Adventure": 0.08, "Fantasy": 0.05}),
            sessions_per_week=5,
            search_propensity=0.50,
            clickiness=1.7,
            completion_propensity=0.42,
            mean_units=1.8,
            genres=tuple(genres),
        ),
    ]


# ----------------------------------------------------------------------------
# Live simulation
# ----------------------------------------------------------------------------
class LiveSim:
    def __init__(self, n_agents: int = 240, seed: int = 7, tick_seconds: float = 1.5):
        self.base_agents = n_agents  # requested count; reset() reuses this
        self.n_agents = n_agents
        self.seed = seed
        self.tick_seconds = tick_seconds
        self.rng = np.random.default_rng(seed)
        self.lock = threading.RLock()

        self.genres = list(GRACENOTE_GENRES)
        self.gidx = {g: i for i, g in enumerate(self.genres)}
        self.personas = generate_personas(n_agents, self.genres, seed=seed)
        # Two hand-built baseball lovers round out the population.
        anchors = _anchor_personas(self.genres)
        self.anchor_ids = [p.persona_id for p in anchors]
        self.personas.extend(anchors)
        n_agents = self.n_agents = len(self.personas)
        self.catalog = _load_catalog_30(REPO_ROOT)
        self.by_id = {it.item_id: it for it in self.catalog.items}
        self.by_tag: dict[str, list] = {}
        for it in self.catalog.items:
            for t in it.genre_tags:
                self.by_tag.setdefault(t, []).append(it)
        # Non-pivot agents mostly watch outside Bollywood: precompute the
        # Bollywood-free pools so their histories stay clean.
        self.by_tag_nobw: dict[str, list] = {
            t: [it for it in items if "Bollywood" not in it.genre_tags]
            for t, items in self.by_tag.items()}
        self.last_update: dict[str, dict] = {}

        # Cold start: near-uniform tastes. Behavior params stay distinct.
        self.tastes = self.rng.dirichlet(np.full(len(self.genres), 5.0), size=n_agents)
        # Agent #7 (index 6): the dedicated Bollywood pivot — a Bollywood-heavy
        # taste vector so the sim always has a desi audience to program for.
        # (Threshold uses the requested count so the pivot never lands on an
        # appended anchor.)
        self.pivot_idx = 6 if self.base_agents > 6 else None
        if self.pivot_idx is not None:
            bw = np.full(len(self.genres), 0.015)
            bw[self.gidx["Bollywood"]] = 0.45
            for g, w in (("Musical", 0.12), ("Romance", 0.12), ("Drama", 0.10),
                         ("Comedy", 0.08)):
                bw[self.gidx[g]] = w
            self.tastes[self.pivot_idx] = bw / bw.sum()
        # The hand-pinned anchors keep their tuned tastes instead of the cold
        # start — the live sim reads self.tastes, not Persona.taste.
        for p in self.personas:
            if p.persona_id in self.anchor_ids:
                self.tastes[self.personas.index(p)] = p.taste / p.taste.sum()
        self.day = 0
        self.paths = [self.tastes.copy()]
        self.events: list[dict] = []          # global recent event feed
        self.agent_events: dict[str, list[dict]] = {p.persona_id: [] for p in self.personas}
        self.watched: dict[str, set[str]] = {p.persona_id: set() for p in self.personas}
        self.genre_plays = np.zeros(len(self.genres))  # live trending counts
        self.day_genre_total = np.zeros(len(self.genres))  # genre counts, latest tick
        self.day_cluster_plays: dict[int, int] = {}        # cluster plays, latest tick

        self.cluster_trend = {name: np.ones(len(self.genres)) / len(self.genres)
                              for name in _ALL_ARCHETYPE_NAMES}
        self.global_trend = np.ones(len(self.genres)) / len(self.genres)

        # Emergent clusters via warm-started k-means on tastes.
        self.k = min(20, n_agents)
        self.centroids = self.tastes[self.rng.choice(n_agents, self.k, replace=False)]
        self.labels = np.zeros(n_agents, dtype=int)
        self._recluster()
        self._refit_projection()
        self._recalc_paths_2d()

        self.running = False
        self.subscribers: list[queue.Queue] = []
        self._stop = threading.Event()

        # Master Agent: natural-language pivot campaigns ("NFL is starting,
        # pivot some users to football"). Each campaign biases the watch
        # sampling of a fixed agent subset toward target genres for N days.
        self.campaigns: list[dict] = []
        self.campaign_history: list[dict] = []  # expired/stopped, for the timeline
        self._camp_seq = 0
        self.master_log: list[dict] = []

        # Pause-mode day stepping (◀ ▶ scrub): per-day state snapshots plus
        # append-only day-stamped logs. Stepping back restores the exact
        # state; stepping forward replays it bit-for-bit from snapshots.
        # Any user mutation (direct/click) truncates the future first.
        self._history: dict[int, dict] = {}
        self._history_cap = 60
        self._watch_log: list[dict] = []       # every watch/search/click event
        self._feed_log: list[dict] = []        # global feed events
        self._master_log_all: list[dict] = []  # master log, uncapped view
        self._all_paths: list[tuple] = []      # (day, tastes.copy())
        self._all_paths.append((0, self.tastes.copy()))
        self._store_snapshot()

    # -- clustering / projection ------------------------------------------------
    def _recluster(self):
        for _ in range(25):
            d2 = ((self.tastes[:, None, :] - self.centroids[None, :, :]) ** 2).sum(-1)
            lab = d2.argmin(1)
            new = np.array([self.tastes[lab == k].mean(0) if (lab == k).any()
                            # reseed dead clusters on a random agent's taste
                            else self.tastes[self.rng.integers(len(self.tastes))]
                            for k in range(self.k)])
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
                "name": f"{top.title()} #{k}",
                "top_genre": top,
                "size": int((self.labels == k).sum()),
            })
        return info

    # -- one simulated day ------------------------------------------------------
    def tick(self):
        rng = self.rng
        bi = self.gidx["Bollywood"]
        day_counts = {name: np.zeros(len(self.genres))
                      for name in _ALL_ARCHETYPE_NAMES}
        day_total = np.zeros(len(self.genres))
        day_cluster: dict[int, int] = {}
        feed = []
        for pi, p in enumerate(self.personas):
            lab = int(self.labels[pi])
            is_piv = self.pivot_idx is not None and pi == self.pivot_idx
            n_watch = int(rng.poisson(4.0) + 1)
            for _ in range(n_watch):
                mix = (0.6 * self.tastes[pi] + 0.25 * self.cluster_trend[p.archetype]
                       + 0.15 * self.global_trend)
                mix = mix / mix.sum()
                # Master Agent campaigns: pivot this agent's sampling toward the
                # campaign genres while the campaign is live.
                camp_bw = False
                for camp in self.campaigns:
                    if pi in camp["targets"] and self._camp_active(camp):
                        # Campaigns fade slowly and steadily: full strength on
                        # day one, linearly down to zero on the last day.
                        w = camp["weight"] * (camp["days_left"] / camp["days_total"])
                        mix = (1 - w) * mix + w * camp["vec"]
                        mix = mix / mix.sum()
                        if "Bollywood" in camp["genres"]:
                            camp_bw = True
                if rng.random() < p.search_propensity:
                    g = self.genres[int(rng.integers(len(self.genres)))]
                    alpha, kind = 0.05, "search"
                else:
                    g = self.genres[int(rng.choice(len(self.genres), p=mix))]
                    alpha, kind = 0.12, "play"
                items = self.by_tag.get(g) or self.catalog.items
                if not is_piv and not camp_bw:
                    # Only agent #7 is the Bollywood guy: everyone else picks
                    # outside Bollywood ~90% of the time (a tiny flavor slips
                    # through). A Master Agent Bollywood campaign overrides this.
                    nobw = self.by_tag_nobw.get(g)
                    if nobw and rng.random() < 0.9:
                        items = nobw
                item = items[int(rng.integers(len(items)))]
                before = float(self.tastes[pi][self.gidx[g]])
                vec = np.asarray(item.genre_vector, dtype=float)
                if not is_piv:
                    # Don't let one casual Bollywood watch rewire a non-pivot
                    # agent's taste: absorb little of its Bollywood axis.
                    vec = vec.copy()
                    vec[bi] *= 0.15
                    s = vec.sum()
                    if s > 0:
                        vec /= s
                self.tastes[pi] = ((1 - alpha) * self.tastes[pi]
                                   + alpha * vec)
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
                self._watch_log.append(ev)
                if len(self._watch_log) > 120000:
                    del self._watch_log[:20000]
                self.last_update[p.persona_id] = {
                    "genre": g, "kind": kind, "title": item.title,
                    "before": round(before, 4), "after": round(after, 4),
                    "day": self.day + 1}
                self.watched[p.persona_id].add(item.item_id)
                day_counts[p.archetype][self.gidx[g]] += 1
                day_total[self.gidx[g]] += 1
                if kind == "play":
                    day_cluster[lab] = day_cluster.get(lab, 0) + 1
                if len(feed) < 6 and rng.random() < 0.3:
                    feed.append(ev)
        for name in _ALL_ARCHETYPE_NAMES:
            tot = day_counts[name].sum()
            if tot > 0:
                self.cluster_trend[name] = (0.5 * self.cluster_trend[name]
                                            + 0.5 * day_counts[name] / tot)
        tot = day_total.sum()
        if tot > 0:
            self.genre_plays += day_total
            self.global_trend = 0.5 * self.global_trend + 0.5 * day_total / tot
        self.day_genre_total = day_total
        self.day_cluster_plays = day_cluster

        self.day += 1
        for camp in self.campaigns:
            if self._camp_active(camp):
                camp["days_left"] -= 1
                if not camp.get("announced"):
                    # a scheduled campaign just went live — fire its toast
                    # and freeze the sim so the visuals can be inspected
                    camp["announced"] = True
                    self.running = False
                    who = f"{len(camp['targets'])} agents"
                    msg = (f"🎭 Master Agent: pivoting {who} toward "
                           f"{', '.join(camp['genres'])} for "
                           f"{camp['days_total']} days. "
                           f"⏸ Sim paused so you can inspect — hit ▶ Play to watch it fade.")
                    self.master_log.append({"day": self.day, "text": msg})
                    self.master_log = self.master_log[-30:]
                    self._master_log_all.append({"day": self.day, "text": msg})
                    if len(self._master_log_all) > 1000:
                        del self._master_log_all[:500]
                    ev = {"kind": "directed", "day": self.day, "text": msg,
                          "n_targets": len(camp["targets"]),
                          "targets": sorted(camp["targets"])}
                    self.events.append(ev)
                    self._feed_log.append(ev)
                    for q in list(self.subscribers):
                        try:
                            q.put_nowait(ev)
                        except queue.Full:
                            pass
        for c in self.campaigns:
            if c["days_left"] <= 0:
                self.campaign_history.append({
                    "id": c["id"], "text": c["text"], "genres": c["genres"],
                    "n_targets": len(c["targets"]),
                    "created_day": c["created_day"],
                    "days_total": c["days_total"], "ended_day": self.day,
                    "live": False})
        self.campaigns = [c for c in self.campaigns if c["days_left"] > 0]
        self.paths.append(self.tastes.copy())
        if len(self.paths) > 61:
            self.paths.pop(0)
        self._all_paths.append((self.day, self.tastes.copy()))
        if len(self._all_paths) > 400:
            del self._all_paths[:100]
        self._recluster()
        self._refit_projection()
        self._recalc_paths_2d()
        self.events.extend(feed)
        self.events = self.events[-200:]
        self._feed_log.extend(feed)
        if len(self._feed_log) > 60000:
            del self._feed_log[:10000]
        for q in list(self.subscribers):
            try:
                q.put_nowait({"kind": "tick", "day": self.day, "feed": feed})
            except queue.Full:
                pass
        self._store_snapshot()

    # -- pause-mode day stepping (◀ ▶) -----------------------------------------
    def _store_snapshot(self):
        self._history[self.day] = {
            "tastes": self.tastes.copy(),
            "watched": {k: set(v) for k, v in self.watched.items()},
            "campaigns": copy.deepcopy(self.campaigns),
            "campaign_history": copy.deepcopy(self.campaign_history),
            "genre_plays": self.genre_plays.copy(),
            "day_genre_total": self.day_genre_total.copy(),
            "day_cluster_plays": dict(self.day_cluster_plays),
            "cluster_trend": {k: v.copy() for k, v in self.cluster_trend.items()},
            "global_trend": self.global_trend.copy(),
            "centroids": self.centroids.copy(),
            "labels": self.labels.copy(),
            "last_update": copy.deepcopy(self.last_update),
            "rng_state": copy.deepcopy(self.rng.bit_generator.state),
        }
        if len(self._history) > self._history_cap:
            for d in sorted(self._history)[:len(self._history) - self._history_cap]:
                del self._history[d]

    def _restore(self, day: int):
        snap = self._history[day]
        self.day = day
        self.tastes = snap["tastes"].copy()
        self.watched = {k: set(v) for k, v in snap["watched"].items()}
        self.campaigns = copy.deepcopy(snap["campaigns"])
        self.campaign_history = copy.deepcopy(snap["campaign_history"])
        self.genre_plays = snap["genre_plays"].copy()
        self.day_genre_total = snap["day_genre_total"].copy()
        self.day_cluster_plays = dict(snap["day_cluster_plays"])
        self.cluster_trend = {k: v.copy()
                              for k, v in snap["cluster_trend"].items()}
        self.global_trend = snap["global_trend"].copy()
        self.centroids = snap["centroids"].copy()
        self.labels = snap["labels"].copy()
        self.last_update = copy.deepcopy(snap["last_update"])
        self.rng.bit_generator.state = copy.deepcopy(snap["rng_state"])
        # Rebuild the day-stamped views as of the restored day.
        self.agent_events = {pid: [] for pid in self.agent_events}
        for e in self._watch_log:
            if e["day"] <= day:
                self.agent_events[e["persona_id"]].append(e)
        for evs in self.agent_events.values():
            if len(evs) > 400:
                del evs[:len(evs) - 400]
        self.events = [e for e in self._feed_log if e["day"] <= day][-200:]
        self.master_log = [e for e in self._master_log_all
                           if e["day"] <= day][-30:]
        self.paths = [t.copy() for (d, t) in self._all_paths if d <= day][-61:]
        self._recluster()
        self._refit_projection()
        self._recalc_paths_2d()

    def _truncate_future(self):
        """Drop any scrubbed-past future: a new user action starts a new branch."""
        if any(d > self.day for d in self._history):
            self._history = {d: s for d, s in self._history.items()
                             if d <= self.day}
            self._watch_log = [e for e in self._watch_log
                               if e["day"] <= self.day]
            self._feed_log = [e for e in self._feed_log
                              if e["day"] <= self.day]
            self._master_log_all = [e for e in self._master_log_all
                                    if e["day"] <= self.day]
            self._all_paths = [(d, t) for (d, t) in self._all_paths
                               if d <= self.day]

    def _broadcast(self, msg: dict):
        for q in list(self.subscribers):
            try:
                q.put_nowait(msg)
            except queue.Full:
                pass

    def step_day(self, direction: int) -> dict:
        """Pause-mode single-day step. +1 forward, -1 backward."""
        if self.running:
            return {"ok": False, "error": "pause the sim to step through days"}
        if direction == 1:
            if self.day + 1 in self._history:
                self._restore(self.day + 1)
                self._broadcast({"kind": "tick", "day": self.day, "feed": []})
            else:
                self.tick()  # stores a snapshot and broadcasts itself
        elif direction == -1:
            if self.day == 0 or (self.day - 1) not in self._history:
                return {"ok": False, "error": "already at the earliest day"}
            self._restore(self.day - 1)
            self._broadcast({"kind": "tick", "day": self.day, "feed": []})
        else:
            return {"ok": False, "error": "direction must be 1 or -1"}
        return {"ok": True, "day": self.day}

    # -- user interaction -------------------------------------------------------
    def click(self, persona_id: str, item_id: str) -> dict:
        with self.lock:
            self._truncate_future()  # a click starts a new timeline branch
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
                self._watch_log.append(ev)
                self._feed_log.append(ev)
            if len(self._watch_log) > 120000:
                del self._watch_log[:20000]
            if len(self._feed_log) > 60000:
                del self._feed_log[:10000]
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
            self._store_snapshot()  # the click is part of this day's state
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
            "is_pivot": pi == self.pivot_idx,
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
        lab = int(self.labels[pi])
        taste = self.tastes[pi]
        # Live campaigns targeting this agent push the campaign genre up the
        # rails too — same fade math as the watch sampling in tick(). The
        # taste bars still show the agent's true taste; this only steers
        # ranking while the campaign is live.
        rank_taste = taste.copy()
        for camp in self.campaigns:
            if pi in camp["targets"] and self._camp_active(camp):
                w = camp["weight"] * (camp["days_left"] / max(camp["days_total"], 1))
                rank_taste = (1 - w) * rank_taste + w * camp["vec"]
                rank_taste = rank_taste / rank_taste.sum()
        seen = self.watched[p.persona_id]
        is_pivot = self.pivot_idx is not None and pi == self.pivot_idx
        # Only agent #7 is the Bollywood guy. Everyone else sees Bollywood
        # titles at a steep ranking discount: a genuine blockbuster can still
        # surface, but Bollywood never reads as a pattern on their rows.
        BW_DISCOUNT = 0.3

        def bwkey(key):
            if is_pivot:
                return key

            def w(it):
                v = key(it)
                if "Bollywood" not in it.genre_tags:
                    return v
                if isinstance(v, tuple):
                    return tuple(x * BW_DISCOUNT for x in v)
                if isinstance(v, str):
                    return ""  # sinks to the tail on reverse sort
                return v * BW_DISCOUNT
            return w

        ranked = [(it, s * (BW_DISCOUNT if (not is_pivot and "Bollywood" in it.genre_tags) else 1.0))
                  for it, s in score_titles(rank_taste, self.catalog)
                  if it.item_id not in seen]
        ranked_items = [it for it, _ in ranked]
        unseen = {it.item_id for it in ranked_items}

        def pool(genre: str) -> list:
            return [it for it in self.by_tag.get(genre, []) if it.item_id in unseen]

        def j(items: list) -> list:
            return [self._item_json(it) for it in items]

        # Per-rail orderings: repeats across rows are fine, identical order is
        # not. Each rail sorts by its own key and tops up from unseen titles
        # in that same key order — never the global taste ranking, which used
        # to make every rail's tail an identical copy.
        score_of = {it.item_id: float(s) for it, s in ranked}
        unseen_items = [it for it in self.catalog.items if it.item_id in unseen]
        k_taste = bwkey(lambda it: score_of.get(it.item_id, 0.0))
        k_pop = bwkey(lambda it: it.popularity)
        k_vote = bwkey(lambda it: it.vote_average)
        k_new = bwkey(lambda it: it.release_date or "")
        k_trend = bwkey(lambda it: (float(self.genre_plays[self.gidx[it.primary_genre]]),
                                    it.popularity))

        def row(pool_items: list, key, reverse: bool = True,
                exclude: set = frozenset(), want: int = n) -> list:
            picked: list = []
            seen_ids = set(exclude)
            for it in sorted(pool_items, key=key, reverse=reverse):
                if it.item_id not in seen_ids:
                    seen_ids.add(it.item_id)
                    picked.append(it)
                if len(picked) >= want:
                    break
            if len(picked) < want:
                for it in sorted(unseen_items, key=key, reverse=reverse):
                    if it.item_id not in seen_ids:
                        seen_ids.add(it.item_id)
                        picked.append(it)
                    if len(picked) >= want:
                        break
            return [self._item_json(x) for x in picked]

        order = np.argsort(rank_taste)[::-1]
        g1, g2 = self.genres[order[0]], self.genres[order[1]]
        evs = self.agent_events[p.persona_id]
        last_play = next((e for e in reversed(evs) if e["type"] == "play"), None)

        rails: list[tuple[str, str, str, list]] = []
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
        cw = list(cont[:n])  # recency order; top up with taste-ranked unseen
        if len(cw) < n:
            have = {it.item_id for it in cw}
            for it in ranked_items:
                if it.item_id not in have:
                    cw.append(it)
                    have.add(it.item_id)
                if len(cw) >= n:
                    break
        rails.append(("continue", "Continue watching", "", j(cw)))
        rails.append(("personalized", "Personalized for you",
                      "ranked live against this agent's Gracenote-genre taste vector",
                      [self._item_json(it, s) for it, s in ranked[:n]]))
        rails.append(("genre", f"Because of your interest in {genre_pretty(g1)}", "",
                      row(pool(g1), k_vote)))
        trending_idx = np.argsort(self.genre_plays)[::-1][:3]
        trending_genres = {self.genres[i] for i in trending_idx if self.genre_plays[i] > 0}
        tr_pool = [it for it in ranked_items if it.primary_genre in trending_genres]
        if tr_pool:
            tr_key = k_trend
        else:
            # Day 0: no daily signal yet. Popular titles weighted by this
            # agent's taste — a different order from "Popular right now".
            tr_pool = [it for it in self.catalog.items if it.item_id in unseen]
            tr_key = lambda it: k_taste(it) * it.popularity
        rails.append(("trending", "Trending now", "what the whole simulation is watching today",
                      row(tr_pool, tr_key)))
        new_cands = [it for it in self.catalog.items
                     if it.item_id in unseen and it.release_date >= "2026-08-01"]
        rails.append(("new", "New this month", "released in the last 60 days",
                      row(new_cands, k_new)))
        if last_play and last_play["item_id"] in self.by_id:
            anchor = np.asarray(self.by_id[last_play["item_id"]].genre_vector,
                                dtype=float)
            k_sim = bwkey(lambda it: float(np.dot(
                anchor, np.asarray(it.genre_vector, dtype=float))))
            bw_pool = [it for it in pool(last_play["genre"])
                       if it.item_id != last_play["item_id"]]
            rails.append(("because_watched", f"Because you watched {last_play['title']}", "",
                          row(bw_pool, k_sim)))
        else:
            # No plays yet: second-favorite genre ordered by rating, so this
            # row never mirrors the Marathon row's popularity order.
            rails.append(("because_watched", f"More {genre_pretty(g2)}", "your second-favorite genre",
                          row(pool(g2), k_vote)))
        pop_all = [it for it in self.catalog.items if it.item_id in unseen]
        rails.append(("popular", "Popular right now", "highest TMDb popularity today",
                      row(pop_all, k_pop)))
        top5 = {self.genres[i] for i in order[:5]}
        detour_pool = [it for it in self.catalog.items
                       if it.item_id in unseen and it.primary_genre not in top5]
        rails.append(("detour", "Worth the detour",
                      "top-rated picks outside your usual genres — variety beats fatigue",
                      row(detour_pool, k_taste)))
        rails.append(("critics", "Critics' picks", "highest rated of all time",
                      row(pop_all, k_vote)))
        fav = self._cluster_favorites(pi, unseen, n)
        if not is_pivot:
            # Cluster favorites are raw play counts — partition so Bollywood
            # doesn't dominate a non-pivot agent's lookalike row.
            fav = ([it for it in fav if "Bollywood" not in it.genre_tags]
                   + [it for it in fav if "Bollywood" in it.genre_tags][:2])
        fav_ids = {it.item_id for it in fav}
        rails.append(("lookalike", "Viewers like you watch",
                      f"most played in cluster {lab} today",
                      j(fav) + row([], k_taste, exclude=fav_ids, want=n - len(fav))))
        hidden_pool = sorted(pop_all, key=k_pop)[:len(pop_all) // 2]
        rails.append(("gems", "Hidden gems for you", "high taste match, low popularity",
                      row(hidden_pool, k_vote)))
        rails.append(("marathon", f"Marathon weekend: {genre_pretty(g2)}", "",
                      row(pool(g2), k_pop)))
        app = p.subscribed_apps[0] if p.subscribed_apps else None
        if app:
            app_pool = [it for it in self.catalog.items
                        if it.item_id in unseen and app in it.providers]
            rails.append(("app", f"On {app}", f"top picks from this agent's {app} subscription",
                          row(app_pool, k_pop)))
        # -- campaign collections: a featured rail per live campaign targeting
        # this agent, pinned near the top while the campaign runs --
        camp_fade = 0.0
        for camp in self.campaigns:
            if pi in camp["targets"] and self._camp_active(camp):
                g = camp["genres"][0]
                fade = camp["days_left"] / max(camp["days_total"], 1)
                camp_fade = max(camp_fade, fade)
                cpool = [it for it in self.by_tag.get(g, [])
                         if it.item_id in unseen]
                # the synthetic MLB shelf leads a Sports collection. Displayed as
                # Baseball (the user-facing directive label); Sports stays the
                # internal taste dimension.
                k_camp = (lambda it: (1 if it.item_id.startswith("tmdb-movie--") else 0,
                                      it.vote_average))
                glabel = "Baseball" if g == "Sports" else genre_pretty(g)
                rails.append(("campaign",
                              f"⚾ Master Agent's {glabel} picks",
                              f"the Master Agent is pivoting you toward "
                              f"{glabel} — {camp['days_left']} days left",
                              row(cpool, k_camp, want=12)))
        # -- dynamic rail ordering: rows rise and fall with the day's activity --
        # Continue watching stays pinned at the top; everything else is scored
        # from live signals each time the home screen is built.
        plays_today = sum(1 for e in evs if e["type"] == "play" and e["day"] == self.day)
        rec = 0.0
        if last_play:
            d = self.day - last_play["day"]
            rec = 1.0 if d <= 0 else (0.5 if d == 1 else 0.15)
        ent = float(-(taste * np.log(taste + 1e-12)).sum() / np.log(len(taste)))
        dts = float(self.day_genre_total.sum())
        conc = float(self.day_genre_total.max() / dts) if dts > 0 else 0.0
        csize = int((self.labels == lab).sum())
        cplays = self.day_cluster_plays.get(lab, 0)
        watched_n = len(self.watched[p.persona_id])
        new_frac = len(new_cands) / max(
            1, sum(1 for it in self.catalog.items if it.release_date >= "2026-08-01"))
        app_share = 0.0
        if app:
            recent = [e for e in reversed(evs) if e["type"] == "play"][:40]
            app_share = (sum(1 for e in recent if e["item_id"] in self.by_id
                             and app in self.by_id[e["item_id"]].providers)
                         / max(1, len(recent)))
        scores = {
            "campaign": 0.55 + 0.40 * camp_fade,  # featured while live, sinks as it fades
            "personalized": 0.60 + 0.15 * min(1.0, plays_today / 4),
            "genre": 0.50 + 0.50 * float(taste[order[0]]),
            "trending": min(1.0, 0.40 + 2.5 * conc),
            "new": 0.45 + 0.30 * new_frac,
            "because_watched": 0.35 + 0.50 * rec,
            "popular": 0.52,
            "detour": 0.35 + 0.50 * (1.0 - ent),
            "critics": 0.48,
            "lookalike": 0.35 + 0.50 * min(1.0, cplays / max(1, csize * 3)),
            "gems": 0.35 + 0.40 * min(1.0, watched_n / 60),
            "marathon": 0.80 if plays_today >= 6 else 0.40,
            "app": 0.40 + 0.40 * app_share,
        }
        ordered = sorted(enumerate(rails),
                         key=lambda pk: (0 if pk[1][0] == "continue" else 1,
                                         -scores.get(pk[1][0], 0.5), pk[0]))
        return {"rails": [{"title": t, "why": w, "items": items}
                          for _, (key, t, w, items) in ordered]}

    def explain(self, persona_id: str, item_id: str) -> dict:
        """Why was this title recommended? Concrete signals, no hand-waving."""
        p = next((pp for pp in self.personas if pp.persona_id == persona_id), None)
        if p is None or item_id not in self.by_id:
            return {"title": "", "signals": []}
        pi = self.personas.index(p)
        taste = self.tastes[pi]
        it = self.by_id[item_id]
        vec = np.asarray(it.genre_vector, dtype=float)
        signals: list[str] = []

        contrib = taste * vec
        for gi in np.argsort(contrib)[::-1][:2]:
            if contrib[gi] > 0.004:
                g = genre_pretty(self.genres[gi])
                signals.append(
                    f"Taste match — your {g} weight is {taste[gi]:.2f} and "
                    f"this title is {vec[gi]:.0%} {g}")

        pg = self.genres[int(np.argmax(vec))]
        plays = [e for e in reversed(self.agent_events[persona_id])
                 if e["type"] == "play" and e["item_id"] != item_id
                 and e["item_id"] in self.by_id
                 and pg in self.by_id[e["item_id"]].genre_tags][:3]
        for e in plays:
            signals.append(
                f"You watched “{e['title']}” on day {e['day']} — it's "
                f"{genre_pretty(pg)} too, the same shelf as this title")
        for e in [x for x in reversed(self.agent_events[persona_id])
                  if x["type"] == "search"][:2]:
            signals.append(
                f"You searched “{e.get('query') or genre_pretty(e['genre'])}” "
                f"on day {e['day']}")

        lab = int(self.labels[pi])
        mates = sum(
            1 for qi, q in enumerate(self.personas)
            if qi != pi and int(self.labels[qi]) == lab
            and any(x["type"] == "play" and x["item_id"] == item_id
                    for x in self.agent_events[q.persona_id][-80:]))
        if mates:
            signals.append(
                f"{mates} viewer{'s' if mates != 1 else ''} in your cluster "
                f"watched this too")

        hs = self.home_screen(p)
        in_rails = [r["title"] for r in hs["rails"]
                    if any(x["id"] == item_id for x in r["items"])]
        if in_rails:
            extra = f" (+{len(in_rails) - 1} more)" if len(in_rails) > 1 else ""
            signals.append(f"Surfaced in “{in_rails[0]}”{extra}")

        for camp in self.campaigns:
            if pi in camp["targets"] and self._camp_active(camp) and any(
                    vec[self.gidx[g]] > 0.25 for g in camp["genres"]):
                signals.append(
                    f"🎭 Master Agent — “{camp['text'][:70]}” is steering "
                    f"viewers like you toward this")
                break

        if it.vote_average:
            signals.append(f"TMDb audience score {it.vote_average:.1f}/10")
        if it.release_date and len(it.release_date) >= 7:
            signals.append(f"Released {it.release_date[:7]}")
        if it.providers:
            signals.append(f"Streaming on {' · '.join(it.providers[:3])}")

        d = self._item_json(it)
        d["signals"] = signals[:8]
        return d

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
        # Sponsored search: exactly one slot. The highest-popularity match for
        # the query is tagged "Sponsored" and inserted at a random position
        # among slots 2-4 (never first), ahead of the personalized organic
        # ranking. The sponsored title is deterministic per query; only the
        # slot is randomized.
        out = []
        if scored:
            spon = max(scored, key=lambda t: t[2].popularity)
            d = self._item_json(spon[2], spon[1])
            d["sponsored"] = True
            rest = [t for t in scored if t[2] is not spon[2]]
            organics = [self._item_json(it, s) for _, s, it in rest[:n - 1]]
            if organics:
                pos = random.randint(1, min(3, len(organics)))
                organics.insert(pos, d)
                out = organics[:n]
            else:
                out = [d]
        return out

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
            self.__init__(n_agents=self.base_agents, seed=seed or self.seed,
                          tick_seconds=self.tick_seconds)

    # -- master agent ---------------------------------------------------------
    def parse_directive(self, text: str) -> dict:
        t = text.lower()
        if re.search(r"\b(stop|cancel|end|kill)\b", t) and "campaign" in t or \
           re.search(r"\b(stop|cancel|end)\s+(all\s+)?campaigns?\b", t):
            return {"action": "stop"}
        genres: list[str] = []
        for kw, g in MASTER_GENRES.items():
            if re.search(r"\b" + re.escape(kw) + r"\b", t) and g not in genres:
                genres.append(g)
        if not genres:
            return {"action": "unknown"}
        m = re.search(r"(\d+)\s*%", t)
        cm = re.search(r"cluster\s*#?\s*(\d+)", t)
        dm = re.search(r"(\d+)\s*days?", t)
        if cm:
            coverage, cluster = 0.0, int(cm.group(1))
        elif re.search(r"\b(all|everyone|everybody)\b", t):
            coverage, cluster = 1.0, None
        elif m:
            coverage, cluster = min(1.0, int(m.group(1)) / 100), None
        elif re.search(r"\bhalf\b", t):
            coverage, cluster = 0.5, None
        elif re.search(r"\bmost\b", t):
            coverage, cluster = 0.6, None
        elif re.search(r"\bfew\b", t):
            coverage, cluster = 0.1, None
        else:  # "some", "a few users", or unspecified
            coverage, cluster = 0.25, None
        # scheduling: "from day 11 to day 20", "starting day 11 for 7 days",
        # "on day 15" (days then come from the "N days" clause or default 7)
        start_day = self.day
        sm = re.search(r"from day (\d+)\s+to day (\d+)", t)
        sm2 = re.search(r"starting day (\d+)\s+for (\d+)\s*days?", t)
        sm3 = re.search(r"\bon day (\d+)\b", t)
        days = int(dm.group(1)) if dm else 7
        if sm:
            start_day, end_day = int(sm.group(1)), int(sm.group(2))
            days = max(end_day - start_day, 1)
        elif sm2:
            start_day, days = int(sm2.group(1)), max(int(sm2.group(2)), 1)
        elif sm3:
            start_day = int(sm3.group(1))
        start_day = max(start_day, self.day)  # past start = fire now
        return {"action": "start", "genres": genres, "coverage": coverage,
                "cluster": cluster, "days": days, "start_day": start_day}

    def _camp_active(self, c: dict) -> bool:
        """A campaign only steers the sim once its start day arrives."""
        return c["start_day"] <= self.day

    def _campaigns_json(self) -> list[dict]:
        return [{"id": c["id"], "text": c["text"], "genres": c["genres"],
                 "n_targets": len(c["targets"]), "targets": sorted(c["targets"]),
                 "weight": c["weight"],
                 "days_left": c["days_left"], "days_total": c["days_total"],
                 "created_day": c["created_day"], "start_day": c["start_day"],
                 "status": "live" if self._camp_active(c) else "scheduled"}
                for c in self.campaigns]

    def direct(self, text: str) -> dict:
        """The manager agent: turn a plain-English directive into a campaign."""
        self._truncate_future()  # a new directive starts a new timeline branch
        parsed = self.parse_directive(text)
        if parsed["action"] == "stop":
            for c in self.campaigns:
                self.campaign_history.append({
                    "id": c["id"], "text": c["text"], "genres": c["genres"],
                    "n_targets": len(c["targets"]),
                    "created_day": c["created_day"],
                    "days_total": c["days_total"], "ended_day": self.day,
                    "live": False})
            n = len(self.campaigns)
            self.campaigns.clear()
            msg = f"Master Agent stopped {n} campaign(s)."
        elif parsed["action"] == "unknown":
            msg = ("Master Agent didn't catch that — try “pivot some users to "
                   "football for 7 days” or “stop campaigns”.")
        else:
            self._camp_seq += 1
            if parsed["cluster"] is not None:
                targets = {i for i in range(self.n_agents)
                           if int(self.labels[i]) == parsed["cluster"]}
            else:
                k = max(1, int(parsed["coverage"] * self.n_agents))
                targets = set(self.rng.choice(
                    self.n_agents, k, replace=False).tolist())
            vec = np.zeros(len(self.genres))
            for g in parsed["genres"]:
                vec[self.gidx[g]] = 1.0 / len(parsed["genres"])
            camp = {"id": self._camp_seq, "text": text,
                    "genres": parsed["genres"], "targets": targets,
                    "vec": vec, "weight": 0.45,
                    "days_left": parsed["days"], "days_total": parsed["days"],
                    "created_day": self.day, "start_day": parsed["start_day"]}
            self.campaigns.append(camp)
            who = (f"cluster {parsed['cluster']}" if parsed["cluster"] is not None
                   else f"{len(targets)} agents")
            if parsed["start_day"] <= self.day:
                msg = (f"🎭 Master Agent: pivoting {who} toward "
                       f"{', '.join(parsed['genres'])} for {parsed['days']} days. "
                       f"⏸ Sim paused so you can inspect — hit ▶ Play to watch it fade.")
                camp["announced"] = True
                # Freeze the sim the moment a campaign goes live: at the
                # default tick a 7-day campaign evaporates in ~11 real seconds,
                # which makes every visual vanish before it can be inspected.
                self.running = False
            else:
                end = parsed["start_day"] + parsed["days"] - 1
                glabels = [("Baseball" if g == "Sports" else genre_pretty(g))
                           for g in parsed["genres"]]
                msg = (f"🎭 Master Agent: scheduled {', '.join(glabels)} "
                       f"for {who} — days {parsed['start_day']}–{end}. "
                       f"Nothing changes on screen until day {parsed['start_day']}.")
                camp["announced"] = False
        self.master_log.append({"day": self.day, "text": msg})
        self.master_log = self.master_log[-30:]
        self._master_log_all.append({"day": self.day, "text": msg})
        if len(self._master_log_all) > 1000:
            del self._master_log_all[:500]
        n_targets = len(targets) if parsed["action"] == "start" else 0
        ev_targets = sorted(targets) if parsed["action"] == "start" else []
        # scheduled campaigns announce quietly — the energy blast fires when
        # the campaign actually goes live (see tick()).
        scheduled = (parsed["action"] == "start"
                     and parsed["start_day"] > self.day)
        ev = {"kind": "directed", "day": self.day, "text": msg,
              "n_targets": 0 if scheduled else n_targets,
              "targets": [] if scheduled else ev_targets,
              "scheduled": scheduled}
        self.events.append(ev)
        self.events = self.events[-200:]
        self._feed_log.append(ev)
        if len(self._feed_log) > 60000:
            del self._feed_log[:10000]
        for q in list(self.subscribers):
            try:
                q.put_nowait(ev)
            except queue.Full:
                pass
        if parsed["action"] != "unknown":
            self._store_snapshot()  # user actions are part of this day's state
        return {"ok": True, "message": msg,
                "campaigns": self._campaigns_json(),
                "log": self.master_log[-10:]}


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
                        "can_step_back": (sim.day - 1) in sim._history,
                        "n_agents": sim.n_agents,
                        "n_clusters": sim.k,
                        "clusters": sim.cluster_info(),
                        "total_events": sum(len(v) for v in sim.agent_events.values()),
                        "genres": [{"key": g, "name": genre_pretty(g),
                                    "titles": len(sim.by_tag.get(g, []))}
                                   for g in sim.genres],
                        "campaigns": sim._campaigns_json(),
                        "campaign_history": sim.campaign_history[-20:],
                        "master_log": sim.master_log[-10:],
                    })
            if route == "/api/agents":
                with sim.lock:
                    agents = []
                    for pi, p in enumerate(sim.personas):
                        agents.append({
                            "id": p.persona_id, "archetype": p.archetype,
                            "archetype_pretty": p.archetype.replace("_", " ").title(),
                            "top_genre": sim.genres[int(sim.tastes[pi].argmax())],
                            "n_plays": len([e for e in sim.agent_events[p.persona_id]
                                            if e["type"] == "play"]),
                            "cluster": int(sim.labels[pi]),
                            "pivot": pi == sim.pivot_idx,
                            # hand-pinned taste anchor, e.g. "Sports"
                            "pin": ANCHOR_PINS.get(p.persona_id),
                            # live campaign genres hitting this agent, if any
                            "targeted": sorted({g for c in sim.campaigns
                                                for g in c["genres"]
                                                if pi in c["targets"]
                                                and sim._camp_active(c)}),
                            # scheduled-but-not-yet-live campaign genres
                            "scheduled": sorted({g for c in sim.campaigns
                                                 for g in c["genres"]
                                                 if pi in c["targets"]
                                                 and not sim._camp_active(c)}),
                        })
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
                        # Master Agent puppet strings: which agents each live
                        # campaign is currently pulling.
                        "campaigns": [
                            {"id": c["id"], "genres": c["genres"],
                             "targets": sorted(c["targets"])}
                            for c in sim.campaigns if sim._camp_active(c)
                        ],
                    })
            if route == "/api/search":
                q = qs.get("q", [""])[0]
                pid = qs.get("agent", [None])[0]
                with sim.lock:
                    return self._json({"results": sim.search(q, pid)})
            if route == "/api/explain":
                pid = qs.get("agent", [None])[0]
                iid = qs.get("item", [None])[0]
                with sim.lock:
                    return self._json(sim.explain(pid, iid))
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
            if url.path == "/api/direct":
                text = data.get("text", "")
                with sim.lock:
                    return self._json(sim.direct(text))
            if url.path == "/api/control":
                action = data.get("action")
                with sim.lock:
                    if action == "play":
                        sim.running = True
                    elif action == "pause":
                        sim.running = False
                    elif action == "step":
                        if sim.running:
                            sim.tick()
                        else:
                            sim.step_day(1)  # paused: restore-or-advance one day
                    elif action == "step_back":
                        r = sim.step_day(-1)
                        return self._json({"ok": r["ok"], "day": sim.day,
                                           "running": sim.running,
                                           "error": r.get("error")})
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
