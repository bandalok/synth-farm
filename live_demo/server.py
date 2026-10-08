#!/usr/bin/env python3
"""Synth Farm LIVE demo server - the real simulation, running in real time.

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
from synth_farm.apps import APPS


# ----------------------------------------------------------------------------
# Gracenote genre taxonomy (video) - the complete list.
#
# Source: Gracenote ScreenPlay Catalog Access docs (devportal.gracenote.com) -
# the industry metadata taxonomy used across streaming/TV platforms.
# All 29 Gracenote video genres are modeled as taste dimensions, exactly as
# Gracenote provides them. Six have no titles in our TMDb catalog
# (Ambient, Erotica, Game-Show, History, News, Western) - they stay in the
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


# Master Agent provider-promo vocabulary: everyday words -> app catalog name.
# Structured as a map so more subscription promotions can be added later.
# Matched BEFORE the genre scan: promo wording carries no genre keyword and
# would otherwise read as "unknown".
PROVIDER_PROMO_KEYWORDS = {
    "foxone": "FOX One",
    "fox one": "FOX One",
    "fox-one": "FOX One",
    "fox 1": "FOX One",
}

#: Title keywords that mark elections/political coverage for the midterms
#: campaign. Matched against catalog titles (lowercased, substring).
ELECTION_KEYWORDS = (
    "election", "president", "white house", "campaign", "vote", "voting",
    "senate", "congress", "politic", "ballot", "midterm", "democrat",
    "republican",
)

#: US tentpole events the Master Agent calendar plans around.
#: (name, month/day label, blurb). Real-world dates; scheduling maps to sim days.
US_TENTPOLES = [
    ("World Series", "Oct 2026", "Fall Classic; baseball's championship series"),
    ("Midterm Elections", "Nov 3, 2026", "political docs and news viewing surge"),
    ("Diwali", "Nov 8, 2026", "festival of lights; family co-viewing, Bollywood surge"),
    ("Thanksgiving Day", "Nov 26, 2026", "family co-viewing peak; holiday movies surge"),
    ("Black Friday", "Nov 27, 2026", "retail frenzy; deal-hunting mindset"),
    ("Christmas Day", "Dec 25, 2026", "biggest co-viewing day of the year"),
    ("New Year's Eve", "Dec 31, 2026", "party viewing; countdown programming"),
    ("College Football Playoff", "Jan 2027", "championship chase; peak college football"),
    ("NFL Playoffs", "Jan 2027", "road to the Super Bowl; football viewing peak"),
    ("Chinese New Year", "Feb 6, 2027", "Year of the Fire Goat; reunion viewing peak across Asia and diaspora"),
    ("Super Bowl LXI", "Feb 14, 2027", "largest US TV audience; sports + ads"),
    ("Valentine's Day", "Feb 14, 2027", "romance titles spike"),
    ("Daytona 500", "Feb 2027", "NASCAR's biggest race; motorsports viewing spike"),
    ("NBA All-Star Weekend", "Feb 2027", "basketball showcase; highlight viewing surge"),
    ("March Madness", "Mar 16 – Apr 6, 2027", "college basketball; sports docuseries halo"),
    ("MLB Opening Day", "Mar 30, 2027", "baseball returns; spring optimism"),
    ("The Masters", "Apr 8 – 11, 2027", "golf's biggest weekend"),
    ("Kentucky Derby", "May 1, 2027", "horse racing's biggest day"),
    ("Cannes Film Festival", "May 2027", "film festival; arthouse and awards titles spike"),
    ("UEFA Champions League Final", "May 2027", "club football climax; massive global sports audience"),
    ("Indianapolis 500", "May 2027", "IndyCar's crown jewel; racing audience peak"),
    ("French Open", "May – Jun 2027", "tennis Grand Slam; clay court drama"),
    ("NBA Finals", "Jun 2027", "basketball climax; sports viewing peak"),
    ("Stanley Cup Finals", "Jun 2027", "hockey's ultimate prize; playoff intensity peak"),
    ("US Open Golf", "Jun 2027", "golf major; championship drama"),
    ("Dragon Boat Festival", "Jun 9, 2027", "Chinese festival; cultural programming moment"),
]

#: Generic labels for home-screen rail kinds (collections management UI).
RAIL_LABELS = {
    "featured": "Featured",
    "apps": "Apps",
    "continue": "Continue watching",
    "personalized": "Personalized for you",
    "genre": "Genre spotlight",
    "trending": "Trending now",
    "new": "New this month",
    "because_watched": "Because you watched",
    "popular": "Popular right now",
    "detour": "Worth the detour",
    "critics": "Critics' picks",
    "lookalike": "Viewers like you watch",
    "gems": "Hidden gems for you",
    "marathon": "Marathon weekend",
    "app": "On your app",
    "campaign": "Master Agent picks",
    "provider_promo": "Promoted subscription",
}

#: Fixed rails - always pinned to the top of the home screen in this order.
FIXED_RAILS = ["featured", "apps", "continue"]

#: The 25 official collections in the rotation pool (beyond the 3 fixed rails).
#: Each entry: (kind, title, description). Status is tracked in collections_state.json.
OFFICIAL_COLLECTIONS = [
    ("personalized", "Personalized for you", "Ranked live against the agent's taste vector"),
    ("genre", "Genre spotlight", "Top picks from the agent's favorite genre"),
    ("trending", "Trending now", "What the whole simulation is watching today"),
    ("new", "New this month", "Released in the last 60 days"),
    ("because_watched", "Because you watched", "Similar to the last played title"),
    ("popular", "Popular right now", "Highest TMDb popularity today"),
    ("detour", "Worth the detour", "Top-rated picks outside usual genres"),
    ("critics", "Critics' picks", "Highest rated of all time"),
    ("lookalike", "Viewers like you watch", "Most played in the agent's cluster"),
    ("gems", "Hidden gems for you", "High taste match, low popularity"),
    ("marathon", "Marathon weekend", "Binge the agent's second-favorite genre"),
    ("app", "On your app", "Top picks from the subscribed app"),
    ("campaign", "Master Agent picks", "Curated by active campaigns"),
    ("provider_promo", "Promoted subscription", "Sponsored app placements"),
    ("bollywood", "Bollywood spotlight", "Hindi cinema picks for the Bollywood axis"),
    ("family", "Family movie night", "Crowd-pleasers for all ages"),
    ("docs", "Documentaries that matter", "Award-level nonfiction"),
    ("binge", "Binge-worthy series", "Shows you'll finish in a weekend"),
    ("comfort", "Comfort rewatches", "Familiar favorites worth revisiting"),
    ("award", "Award winners", "Oscar, Emmy and festival honorees"),
    ("indie", "Indie discoveries", "Festival gems and breakout debuts"),
    ("throwback", "Throwback classics", "Timeless films from the vault"),
    ("shorts", "Quick watches", "Under 90 minutes, zero commitment"),
    ("international", "Around the world", "Standouts from global cinema"),
    ("scifi", "Sci-fi adventures", "Futures worth exploring"),
]

#: AI-generated collection candidates awaiting human approval.
#: Each entry: (id, title, description, recommendation).
AI_COLLECTION_CANDIDATES = [
    ("ai_rainy", "Rainy day marathons", "Cozy viewing for bad weather weekends",
     "Weather-correlated viewing spikes 34% on rainy weekends; fills a mood gap no official collection covers."),
    ("ai_director", "Director spotlight", "Auteur retrospectives, one filmmaker at a time",
     "Auteur-driven discovery drives 2.1x deeper catalog exploration than genre rows."),
    ("ai_true", "Based on true stories", "Real events, dramatized",
     "True-story titles over-index 28% on completion rate across all clusters."),
    ("ai_mindbender", "Mind-bending sci-fi", "Movies that rewire your brain",
     "Sci-fi completion is top-quartile; a dedicated mind-bender row captures the high-engagement tail."),
    ("ai_feelgood", "Feel-good comedies", "Pure joy, no stakes",
     "Comfort viewing peaks Sunday evenings; no official collection targets this window."),
    ("ai_truecrime", "True crime deep dives", "Cases, con artists and courtrooms",
     "True crime is the #1 documentary subgenre by watch time but has no dedicated row."),
    ("ai_kdrama", "K-drama essentials", "The best of Korean television",
     "K-drama demand up 3x in two years; international row is too broad to serve it."),
    ("ai_anime", "Anime for beginners", "Gateway series for the curious",
     "Anime-curious agents bounce off the main catalog; a starter row lowers the barrier."),
    ("ai_snubs", "Oscar snubs", "Great films the Academy missed",
     "Contrarian curation drives social sharing; critics row only covers winners."),
    ("ai_oneseson", "One-season wonders", "Cancelled too soon, loved forever",
     "Nostalgia rows perform well; this targets the passionate cancelled-show fandoms."),
    ("ai_cult", "Cult classics", "Midnight movies and fan favorites",
     "Cult titles have the highest rewatch rate in the catalog."),
    ("ai_musical", "Musicals that slap", "Song, dance and spectacle",
     "Musical engagement is bimodal - lovers binge; a dedicated row finds them."),
    ("ai_heist", "Heist movies ranked", "The perfect crime, every time",
     "Heist is a top-5 thriller subgenre with zero dedicated coverage."),
    ("ai_space", "Space operas", "Epic sagas among the stars",
     "Space opera overlaps sci-fi adventures but deserves its epic-scale framing."),
    ("ai_cozy", "Cozy mysteries", "Gentle crimes, charming detectives",
     "Cozy mystery viewing is up 40% YoY; detour row is too broad."),
    ("ai_dystopia", "Dystopian futures", "Bleak worlds, gripping stories",
     "Dystopia indexes high with 18-34; no official row isolates it."),
    ("ai_roadtrip", "Road trip movies", "Journeys that change everything",
     "Thematic rows outperform generic genre rows on click-through."),
    ("ai_whodunit", "Whodunits", "Guess the killer before the reveal",
     "Mystery completion rates justify a dedicated whodunit row."),
    ("ai_superhero", "Superhero origins", "Where legends begin",
     "Superhero fatigue is real but origin stories still open strong."),
    ("ai_epic", "Historical epics", "Sweeping stories from history",
     "Epic runtimes need intentional framing; award row buries them."),
    ("ai_mock", "Mockumentaries", "Fiction so dry it's real",
     "Comedy subgenre with cult following; no official coverage."),
    ("ai_slowburn", "Slow burn thrillers", "Tension that builds and builds",
     "Slow-burn titles get abandoned without the right framing and expectations."),
    ("ai_prestige", "Prestige TV", "The dramas everyone talks about",
     "Water-cooler shows drive subscription value; worth a dedicated row."),
    ("ai_guilty", "Guilty pleasures", "Reality TV and lowbrow gold",
     "Guilty-pleasure viewing is high but hidden; naming it normalizes the habit."),
    ("ai_standup", "Stand-up specials", "Laugh in under an hour",
     "Comedy specials are the highest-completion shortform in the catalog."),
    ("ai_nature", "Nature documentaries", "Planet Earth and beyond",
     "Nature docs over-index on family co-viewing; docs row is too serious."),
    ("ai_sports", "Sports stories", "Underdogs and dynasties",
     "Sports narratives cross genre lines; a dedicated row captures the fandom."),
    ("ai_time", "Time travel tales", "Past, present and paradox",
     "Time-travel is a top sci-fi trope search with no dedicated row."),
    ("ai_halloween", "Halloween horror", "Scares for spooky season",
     "October demand spike: horror viewing triples in the two weeks before Halloween."),
    ("ai_thanksgiving", "Thanksgiving comfort", "Family films for the holiday",
     "Holiday co-viewing peaks Thanksgiving week; a family-friendly row captures it."),
    ("ai_midterms", "Election season", "Political thrillers and documentaries",
     "Midterms Nov 3: political viewing surges in the two weeks before election day."),
    ("ai_baseball", "Baseball classics", "America's pastime on screen",
     "World Series month: baseball titles get a championship halo."),
]

COLLECTIONS_STATE_FILE = "data/collections_state.json"

#: AI candidates tied to imminent events - shown with a flashing
#: "highly recommended" tag in the seasonality table.
HOT_AI_CANDIDATES = {"ai_halloween", "ai_thanksgiving", "ai_midterms", "ai_baseball"}

#: Theme genres for AI-approved collections, used to build their homepage
#: rails. Collections without an entry fall back to popular titles.
AI_COLLECTION_THEMES = {
    "ai_thanksgiving": ["Family", "Animation", "Comedy"],
    "ai_halloween": ["Horror", "Thriller", "Mystery"],
    "ai_feelgood": ["Comedy", "Romantic Comedy", "Family"],
    "ai_rainy": ["Drama", "Romance", "Mystery"],
    "ai_cozy": ["Mystery", "Comedy", "Romance"],
    "ai_truecrime": ["Documentary", "Crime", "Mystery"],
    "ai_kdrama": ["Drama", "Romance"],
    "ai_anime": ["Animation"],
    "ai_heist": ["Crime", "Thriller", "Action"],
    "ai_space": ["Science Fiction", "Adventure"],
    "ai_mindbender": ["Science Fiction", "Thriller", "Mystery"],
    "ai_dystopia": ["Science Fiction", "Thriller"],
    "ai_cult": ["Comedy", "Horror"],
    "ai_musical": ["Musical", "Music"],
    "ai_roadtrip": ["Adventure", "Comedy", "Drama"],
    "ai_whodunit": ["Mystery", "Crime"],
    "ai_superhero": ["Action", "Adventure", "Science Fiction"],
    "ai_epic": ["History", "War", "Drama"],
    "ai_mock": ["Comedy", "Documentary"],
    "ai_slowburn": ["Thriller", "Mystery", "Drama"],
    "ai_prestige": ["Drama"],
    "ai_director": ["Drama"],
    "ai_true": ["Biography", "Documentary", "History"],
    "ai_snubs": ["Drama"],
    "ai_oneseson": ["Drama", "Comedy"],
    "ai_midterms": ["News", "Documentary", "Drama"],
    "ai_baseball": ["Sports", "Documentary"],
}


def _default_collections_state() -> dict:
    return {
        "official": [
            {"id": kind, "title": title, "description": desc,
             "fixed": kind in FIXED_RAILS,
             "status": "active",  # active | deprecated
             }
            for kind, title, desc in ([(k, RAIL_LABELS[k], "") for k in FIXED_RAILS]
                                      + OFFICIAL_COLLECTIONS)
        ],
        "removed": [],  # ids removed from the pool (restorable)
        "ai_candidates": [
            {"id": cid, "title": title, "description": desc,
             "recommendation": rec, "status": "pending",  # pending | approved | rejected
             "hot": cid in HOT_AI_CANDIDATES}
            for cid, title, desc, rec in AI_COLLECTION_CANDIDATES
        ],
    }


def _load_collections_state(repo_root: str) -> dict:
    path = os.path.join(repo_root, COLLECTIONS_STATE_FILE)
    try:
        with open(path) as fh:
            state = json.load(fh)
        # merge in any new official/AI entries added since the file was written
        default = _default_collections_state()
        have = {c["id"] for c in state.get("official", [])} | set(state.get("removed", []))
        for c in default["official"]:
            if c["id"] not in have:
                state.setdefault("official", []).append(c)
        have_ai = {c["id"] for c in state.get("ai_candidates", [])}
        for c in default["ai_candidates"]:
            if c["id"] not in have_ai:
                state.setdefault("ai_candidates", []).append(c)
            else:
                # backfill the hot flag on existing entries
                for e in state["ai_candidates"]:
                    if e["id"] == c["id"]:
                        e["hot"] = c["hot"]
        return state
    except (OSError, ValueError):
        return _default_collections_state()


def _save_collections_state(repo_root: str, state: dict) -> None:
    path = os.path.join(repo_root, COLLECTIONS_STATE_FILE)
    with open(path, "w") as fh:
        json.dump(state, fh, indent=2)


def _coverage(data) -> float:
    try:
        return max(0.0, min(1.0, float(data.get("coverage", 0.25))))
    except (TypeError, ValueError):
        return 0.25


def _cluster(data):
    c = data.get("cluster")
    try:
        return int(c) if c is not None and str(c).strip() != "" else None
    except (TypeError, ValueError):
        return None


def _days(data) -> int:
    try:
        return max(1, int(data.get("days", 7)))
    except (TypeError, ValueError):
        return 7


def _start_day(data, sim):
    s = data.get("start_day")
    try:
        return max(sim.day, int(s)) if s is not None else None
    except (TypeError, ValueError):
        return None


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
    # The 16-title MLB shelf below needs room: drop the 16 weakest fixture
    # entries - anything posterless first, then the lowest-popularity
    # non-Hindi titles (the Hindi set is protected for the Bollywood
    # dimension). Titles listed on FOX One are protected too - they back
    # the FOX One subscription-promotion campaigns.
    drop_ids: set = set()
    for e in entries:
        if not e.get("poster_path"):
            drop_ids.add(e["tmdb_id"])

    def _fox_one_listed(e: dict) -> bool:
        return any("FOX One" in (p or "")
                   for p in e.get("providers_flatrate", []))

    non_hi = sorted((e for e in entries if e["tmdb_id"] not in drop_ids
                     and e.get("original_language") != "hi"
                     and not _fox_one_listed(e)),
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
    assert len(entries) >= 700, f"catalog must be at least 700, got {len(entries)}"
    items = [_tmdb_entry_to_item(e) for e in entries]
    return SimpleNamespace(items=items)


def _tmdb_entry_to_item(e: dict) -> SimpleNamespace:
    """Build one catalog item from a TMDB fixture entry, in the 30-genre
    Gracenote space. Shared by the main catalog and the promo-only catalog."""
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
    return SimpleNamespace(
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
        backdrop_path=str(e.get("backdrop_path") or ""),
        providers=tuple(providers),
    )


def _load_promo_catalog(repo_root: str) -> SimpleNamespace:
    """Promo-only catalog (e.g. Britbox launch titles). Same item shape as
    the main catalog, but kept separate so these titles never leak into
    rails, search, or cold start - they surface solely via app promotions."""
    path = os.path.join(repo_root, "data", "catalog_britbox.json")
    if not os.path.exists(path):
        return SimpleNamespace(items=[])
    entries = json.load(open(path))["items"]
    return SimpleNamespace(items=[_tmdb_entry_to_item(e) for e in entries])


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
    - social_fan: here for the hangout. Sports plus comedy/reality - the
      Friday-night crowd. Searches, samples, bails early.
    - scifi_purist ("the voidwalker"): lives in deep space. Science Fiction
      with a mystery/thriller bench. Binges whole series, finishes everything.
    - scifi_tourist: here for the spectacle. Sci-fi plus comedy/action - the
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
        # Promo-only catalog: titles that exist ONLY for app-promotion
        # campaigns (e.g. the Britbox launch). Never merged into the main
        # catalog, so they never appear in rails, search, or cold start -
        # they surface solely through provider_promo campaigns.
        self.promo_by_id: dict = {}
        _promo = _load_promo_catalog(REPO_ROOT)
        self.promo_by_id = {it.item_id: it for it in _promo.items}
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
        # Agent #7 (index 6): the dedicated Bollywood pivot - a Bollywood-heavy
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
        # Agent #8 (index 7): the dedicated Sports pivot - a sports-heavy
        # taste vector from day 1 so the sim always has a sports diehard
        # among the regular population. Mirrors the Bollywood pivot exactly
        # (same spike math, same 0.015 base); the hand-pinned sports anchors
        # stay untouched and remain the two heaviest sports users.
        # (Same guard as above: the requested count keeps the pivot off the
        # appended anchors.)
        self.sports_pivot_idx = 7 if self.base_agents > 7 else None
        if self.sports_pivot_idx is not None:
            sw = np.full(len(self.genres), 0.015)
            sw[self.gidx["Sports"]] = 0.45
            for g, w in (("Documentary", 0.12), ("Drama", 0.10),
                         ("Comedy", 0.08), ("Reality", 0.08)):
                sw[self.gidx[g]] = w
            self.tastes[self.sports_pivot_idx] = sw / sw.sum()
        # The hand-pinned anchors keep their tuned tastes instead of the cold
        # start - the live sim reads self.tastes, not Persona.taste.
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
        # Collections management: {"hidden": [rail kinds], "order": [rail kinds],
        # "pinned": {kind: row}}.
        # Empty = fully dynamic (default rail order + scoring).
        self.rail_config: dict = {"hidden": [], "order": [], "pinned": {}}
        # Collections registry: official pool + AI candidates, persisted to JSON.
        self.collections_state: dict = _load_collections_state(REPO_ROOT)
        self.master_log: list[dict] = []

        # Pause-mode day stepping (◀ ▶ scrub): per-day state snapshots plus
        # append-only day-stamped logs. Stepping back restores the exact
        # state; stepping forward replays it bit-for-bit from snapshots.
        # Any user mutation (direct/click) truncates the future first.
        self._history: dict[int, dict] = {}
        self._history_cap = 60
        self._watch_log: list[dict] = []       # every watch/search/click event
        self._query_log: list[dict] = []       # frontend search queries {day, q}
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
                    # A paid placement (vec is None, weight 0.0) must NEVER
                    # modify taste vectors.
                    if (pi in camp["targets"] and self._camp_active(camp)
                            and camp.get("vec") is not None):
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
                    # a scheduled campaign just went live - fire its toast
                    # and freeze the sim so the visuals can be inspected
                    camp["announced"] = True
                    self.running = False
                    who = f"{len(camp['targets'])} agents"
                    if camp.get("campaign_type") == "provider_promo":
                        msg = (f"Master Agent: running a {camp['provider']} "
                               f"subscription promotion for {who} - "
                               f"{camp['days_total']} days. "
                               f"⏸ Sim paused so you can inspect - hit ▶ Play to watch it run.")
                    else:
                        msg = (f"Master Agent: pivoting {who} toward "
                               f"{', '.join(camp['genres'])} for "
                               f"{camp['days_total']} days. "
                               f"⏸ Sim paused so you can inspect - hit ▶ Play to watch it fade.")
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
                    "campaign_type": c.get("campaign_type", "genre_pivot"),
                    "provider": c.get("provider"),
                    "titles": sorted(c.get("titles") or []),
                    "n_targets": len(c["targets"]),
                    "targets": sorted(c["targets"]),
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
            item = self._resolve_item(item_id)
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
            # Subscription-promotion conversion: clicking a promoted title
            # can turn a targeted non-subscriber into a subscriber. Modest,
            # deterministic via the sim RNG; a paid placement still never
            # touches taste (see the gated loops in tick()/home_screen()).
            for camp in self.campaigns:
                if (camp.get("campaign_type") != "provider_promo"
                        or pi not in camp["targets"]
                        or not self._camp_active(camp)
                        or item_id not in (camp.get("titles") or ())):
                    continue
                provider = camp["provider"]
                if (provider in p.subscribed_apps
                        or self.rng.random()
                        >= 0.12 * max(p.clickiness, 0.2)):
                    continue
                p.subscribed_apps = tuple(p.subscribed_apps) + (provider,)
                aff = getattr(p, "app_affinity", None)
                if aff is not None and len(aff) == len(p.subscribed_apps) - 1:
                    # app_affinity is positionally aligned to subscribed_apps:
                    # the new app enters at mean affinity, then renormalize.
                    ext = np.append(np.asarray(aff, dtype=float),
                                    float(np.mean(aff)))
                    s = float(ext.sum())
                    p.app_affinity = (ext / s if s > 0
                                      else np.ones_like(ext) / len(ext))
                conv = {"day": self.day, "persona_id": persona_id,
                        "archetype": p.archetype, "type": "conversion",
                        "item_id": item_id, "title": item.title,
                        "genre": item.primary_genre,
                        "campaign_id": camp["id"],
                        "provider": provider}
                evs.append(conv)
                self.events.append(conv)
                self._watch_log.append(conv)
                self._feed_log.append(conv)
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
            "is_sports_pivot": pi == self.sports_pivot_idx,
            "home_screen": self.home_screen(p),
        }

    def _resolve_item(self, item_id: str):
        """Resolve a title from the main catalog or the promo-only catalog."""
        return self.by_id.get(item_id, self.promo_by_id.get(item_id))

    def _provider_titles(self, provider: str) -> set:
        """All title IDs streaming on a provider: main catalog plus the
        promo-only catalog (e.g. Britbox launch titles), which never
        appears in rails/search and surfaces solely via app promotions."""
        titles = {it.item_id for it in self.catalog.items
                  if provider in it.providers}
        titles |= {it.item_id for it in self.promo_by_id.values()
                   if provider in it.providers}
        return titles

    def _election_titles(self) -> list:
        """Title IDs for elections/political coverage: keyword matched
        against the main catalog, most popular first."""
        scored = []
        for it in self.catalog.items:
            tl = it.title.lower()
            if any(kw in tl for kw in ELECTION_KEYWORDS):
                scored.append((it.popularity, it.item_id))
        scored.sort(reverse=True)
        return [tid for _, tid in scored]

    def _item_json(self, item, score=None):
        d = {"id": item.item_id, "title": item.title,
             "genre": genre_pretty(item.primary_genre),
             "poster": (f"https://image.tmdb.org/t/p/w342{item.poster_path}"
                        if item.poster_path else ""),
             "backdrop": (f"https://image.tmdb.org/t/p/w780{getattr(item, 'backdrop_path', '')}"
                          if getattr(item, "backdrop_path", "") else ""),
             "providers": list(item.providers),
             "promoted": False}
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
        # rails too - same fade math as the watch sampling in tick(). The
        # taste bars still show the agent's true taste; this only steers
        # ranking while the campaign is live.
        rank_taste = taste.copy()
        for camp in self.campaigns:
            # Paid placements never steer ranking either (vec is None).
            if (pi in camp["targets"] and self._camp_active(camp)
                    and camp.get("vec") is not None):
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
        # in that same key order - never the global taste ranking, which used
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
        # -- Featured rail: top taste-ranked unseen titles. Active "featured"
        # campaigns targeting this agent pin their titles first. --
        pinned = []
        pinned_ids: set = set()
        for camp in self.campaigns:
            if (camp.get("campaign_type") == "featured"
                    and pi in camp["targets"] and self._camp_active(camp)):
                for tid in sorted(camp.get("titles") or ()):
                    if tid in self.by_id and tid not in pinned_ids:
                        pinned.append(self.by_id[tid])
                        pinned_ids.add(tid)
        feat_items = []
        for it in pinned:
            tile = self._item_json(it)
            tile["promoted"] = True
            feat_items.append(tile)
        for it, s in ranked:
            if it.item_id not in pinned_ids:
                feat_items.append(self._item_json(it, s))
                pinned_ids.add(it.item_id)
            if len(feat_items) >= 10:
                break
        # -- Featured placement: a live provider_promo campaign with "featured"
        # checked drops one promoted app tile at a random spot in the Featured
        # row, tagged "promoted". Deterministic per (agent, day, campaign). --
        for camp in self.campaigns:
            if (camp.get("campaign_type") != "provider_promo"
                    or pi not in camp.get("targets", ())
                    or not self._camp_active(camp)
                    or "featured" not in camp.get("placements", ["collection"])):
                continue
            prov = camp.get("provider")
            prov_titles = [self.by_id[tid] for tid in camp.get("titles", ())
                           if tid in self.by_id]
            exclusive = [it for it in prov_titles
                         if tuple(it.providers) == (prov,)]
            cand_pool = exclusive or prov_titles
            if not cand_pool:
                continue
            frng = random.Random(f"feat-{pi}-{self.day}-{camp['id']}")
            pick = frng.choice(cand_pool)
            tile = self._item_json(pick)
            tile["promoted"] = True
            tile["promoted_by"] = prov
            slot = frng.randint(0, len(feat_items))
            feat_items.insert(slot, tile)
            break
        # -- Elections coverage: a live elections campaign puts a synthetic
        # LIVE tile first in the Featured row (Peacock branding, display
        # only) for targeted agents. --
        for camp in self.campaigns:
            if (camp.get("campaign_type") != "elections"
                    or pi not in camp.get("targets", ())
                    or not self._camp_active(camp)):
                continue
            live_tile = {
                "id": f"live-elections-{camp['id']}",
                "title": "Election Coverage Live",
                "genre": "News",
                "poster": "",
                "backdrop": "",
                "providers": ["Peacock"],
                "promoted": False,
                "live": True,
                "why_hover": ("Live election coverage - the Master Agent is running "
                              f"an elections push ({camp['days_left']} days left)"),
            }
            feat_items.insert(0, live_tile)
            break
        rails.append(("featured", "Featured", "top picks for this agent today",
                      feat_items))
        rails.append(("apps", "Apps", "streaming apps on this TV",
                      [{"app": name} for name in APPS]))
        rails.append(("continue", "Continue watching", "", j(cw)))
        # -- Elections coverage rail: pinned at position 4, right after the
        # three fixed rails, for agents targeted by a live elections campaign.
        for camp in self.campaigns:
            if (camp.get("campaign_type") != "elections"
                    or pi not in camp.get("targets", ())
                    or not self._camp_active(camp)):
                continue
            elect_items = []
            for tid in camp.get("titles", ()):
                it = self._resolve_item(tid)
                if it is not None:
                    d = self._item_json(it)
                    d["why_hover"] = ("Election coverage - picked by the Master Agent's "
                                      "elections push")
                    elect_items.append(d)
                if len(elect_items) >= 10:
                    break
            if elect_items:
                rails.append(("elections", "Election coverage",
                              "live results, docs and political titles",
                              elect_items))
            break
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
            # agent's taste - a different order from "Popular right now".
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
                      "top-rated picks outside your usual genres - variety beats fatigue",
                      row(detour_pool, k_taste)))
        rails.append(("critics", "Critics' picks", "highest rated of all time",
                      row(pop_all, k_vote)))
        fav = self._cluster_favorites(pi, unseen, n)
        if not is_pivot:
            # Cluster favorites are raw play counts - partition so Bollywood
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
            if (pi in camp["targets"] and self._camp_active(camp)
                    and camp.get("vec") is not None):
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
                              f"{glabel} - {camp['days_left']} days left",
                              row(cpool, k_camp, want=12)))
        # -- AI-approved collections: each approved collection becomes a
        # functional rail, visible from the day after approval onwards. --
        try:
            cst = self.collections_state
            for c in cst.get("official", []):
                if not c.get("ai_approved") or c.get("status") != "active":
                    continue
                if self.day <= c.get("approved_day", -1):
                    continue
                cid = c["id"]
                themes = AI_COLLECTION_THEMES.get(cid)
                if themes:
                    cpool = [it for it in self.catalog.items
                             if it.item_id in unseen and it.primary_genre in themes]
                else:
                    cpool = [it for it in self.catalog.items
                             if it.item_id in unseen]
                if not cpool:
                    continue
                rails.append((f"ai_{cid}", c["title"],
                              c.get("description", ""),
                              row(cpool, k_pop)))
        except AttributeError:
            pass
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
        # -- rail order: Featured and Apps lead, Continue watching stays
        # pinned third, everything else is scored from live signals --
        _PIN = {"featured": 0, "apps": 1, "continue": 2}
        ordered = sorted(enumerate(rails),
                         key=lambda pk: (_PIN.get(pk[1][0], 3),
                                         -scores.get(pk[1][0], 0.5), pk[0]))
        # -- collections management: hide rails and/or apply a custom order --
        hidden = set(self.rail_config.get("hidden", []))
        custom = [k for k in self.rail_config.get("order", [])]
        # -- collections registry: drop deprecated/removed collections --
        try:
            cst = self.collections_state
            deprecated = {c["id"] for c in cst.get("official", [])
                          if c.get("status") == "deprecated"}
            removed = set(cst.get("removed", []))
            hidden |= deprecated | removed
        except AttributeError:
            pass
        if hidden or custom:
            kept = [(key, t, w, items) for _, (key, t, w, items) in ordered
                    if key not in hidden]
            if custom:
                pos = {k: i for i, k in enumerate(custom)}
                kept = sorted(enumerate(kept),
                              key=lambda pk: (pos.get(pk[1][0], len(pos)), pk[0]))
                kept = [r for _, r in kept]
            ordered = list(enumerate(kept))
        # -- collections circle: fixed 1-2-3 stay pinned; the remaining
        # approved collections rotate through a window that cycles each day.
        # Skipped when the user has set an explicit custom order. --
        if not custom:
            fixed_keys = {"featured", "apps", "continue"}
            fixed_rails = [(i, r) for i, r in ordered if r[0] in fixed_keys]
            pool = [(i, r) for i, r in ordered if r[0] not in fixed_keys]
            # elections coverage pins at position 4, ahead of the rotation
            # pool, while its campaign is live.
            elect = [(i, r) for i, r in pool if r[0] == "elections"]
            pool = [(i, r) for i, r in pool if r[0] != "elections"]
            # rotation only kicks in once the pool outgrows the window;
            # with the current ~12 built rails everything still shows.
            # The pool is already score-ordered; the window preserves it.
            window = 12
            if len(pool) > window:
                start = (self.day * 3) % len(pool)
                circled = [pool[(start + k) % len(pool)] for k in range(window)]
                ordered = fixed_rails + elect + circled
            else:
                ordered = fixed_rails + elect + pool
        # -- user-pinned rows: floating collections pinned at specific
        # 1-indexed row numbers (4+). Fixed 1-3 cannot be overridden. --
        pinned = self.rail_config.get("pinned", {})
        if pinned:
            by_kind = {r[1][0]: r for r in ordered}
            fixed_kinds = {"featured", "apps", "continue"}
            # validate: positions >= 4, kind exists and not hidden/fixed
            valid = {}
            for kind, pos in pinned.items():
                try:
                    pos = int(pos)
                except (TypeError, ValueError):
                    continue
                if pos < 4 or kind in fixed_kinds or kind not in by_kind:
                    continue
                valid[kind] = pos
            if valid:
                # remove pinned from current order, then reinsert at positions
                rest = [r for r in ordered if r[1][0] not in valid]
                # build final list: walk positions 1..N, placing pinned
                # kinds at their slots, filling gaps with the rest in order
                pinned_at = {}
                for kind, pos in valid.items():
                    pinned_at.setdefault(pos, []).append(kind)
                final = []
                rest_iter = iter(rest)
                pos = 1
                # total slots = rest + pinned
                total = len(rest) + len(valid)
                while len(final) < total:
                    if pos in pinned_at:
                        for kind in pinned_at[pos]:
                            final.append(by_kind[kind])
                    else:
                        try:
                            final.append(next(rest_iter))
                        except StopIteration:
                            # no more rest; place remaining pinned
                            for p, kinds in sorted(pinned_at.items()):
                                if p >= pos:
                                    for kind in kinds:
                                        if by_kind[kind] not in final:
                                            final.append(by_kind[kind])
                            break
                    pos += 1
                ordered = [(i, r[1]) for i, r in enumerate(final)]
        annotated = []
        for _, (key, t, w, items) in ordered:
            for d in items:
                if d.get("id"):
                    it = self.by_id.get(d["id"])
                    if it is not None:
                        d["why_hover"] = self._why_hover(pi, taste, rank_taste, it)
            annotated.append({"kind": key, "title": t, "why": w, "items": items})
        # -- subscription-promotion rail: a paid placement shown ONLY to
        # agents targeted by a live provider_promo campaign (conditional, so
        # the no-campaign home screen is unchanged). Pinned to the campaign's
        # FOX One titles, unseen first, no within-rail duplicates. Placement
        # is never 0, 1, or 2 - Featured, Apps, and Continue Watching are
        # never displaced. The slot is random per agent but deterministic per
        # (agent, day, campaign) via a dedicated RNG, so the sim's own RNG
        # stream is undisturbed.
        for camp in self.campaigns:
            if (camp.get("campaign_type") != "provider_promo"
                    or pi not in camp["targets"]
                    or not self._camp_active(camp)):
                continue
            pinned = [self._resolve_item(tid) for tid in camp.get("titles", ())]
            pinned = [it for it in pinned if it is not None]
            # Collection placement: show only titles exclusive to the promoted
            # app - nothing shared with other providers.
            if "collection" in camp.get("placements", ["collection"]):
                exclusive = [it for it in pinned
                             if tuple(it.providers) == (camp["provider"],)]
                if exclusive:
                    pinned = exclusive
            if not pinned:
                continue
            k_unseen_pop = lambda it: (it.item_id in unseen, it.popularity)
            items = row(pinned, k_unseen_pop, want=len(pinned))
            for d in items:
                it = self._resolve_item(d["id"])
                if it is not None:
                    d["why_hover"] = self._why_hover(pi, taste, rank_taste, it)
            rrng = random.Random(f"{pi}-{self.day}-{camp['id']}")
            annotated.insert(rrng.randint(3, len(annotated)),
                             {"kind": "provider_promo",
                              "title": f"Trending on {camp['provider']}",
                              "why": (f"promoted - the Master Agent is running "
                                      f"a {camp['provider']} subscription "
                                      f"push ({camp['days_left']} days left)"),
                              "items": items})
            camp["impressions"].add((pi, self.day))
        # -- screen takeover: full-screen promo for targeted agents --
        takeover = None
        for camp in self.campaigns:
            if (camp.get("campaign_type") == "takeover"
                    and pi in camp["targets"] and self._camp_active(camp)
                    and camp.get("title_id") in self.by_id):
                art = self._item_json(self.by_id[camp["title_id"]])
                art["headline"] = camp.get("headline", "")
                art["campaign_id"] = camp["id"]
                art["sponsor"] = camp.get("sponsor", "")
                # track the view for sponsor ROI
                camp.setdefault("takeover_views", set()).add((pi, self.day))
                takeover = art
                break
        return {"rails": annotated, "takeover": takeover}

    # -- cold start ---------------------------------------------------------------
    def coldstart_taste(self, picks: list[str]) -> np.ndarray:
        """Bootstrapped taste vector from quiz answers. Zero watch history -
        the cold-start solve: 3 answers stand in for months of viewing data.
        Unknown genre keys are ignored, never fatal."""
        w = np.full(len(self.genres), 0.02)
        for i, g in enumerate(picks[:3]):
            if g in self.gidx:
                w[self.gidx[g]] += (0.50, 0.30, 0.20)[min(i, 2)]
        return w / w.sum()

    def coldstart_home(self, taste: np.ndarray, n: int = 20) -> dict:
        """Home screen for a brand-new viewer: quiz-derived taste, empty
        history, no campaign targeting. Mirrors the real home screen's rails
        so the bootstrap is directly comparable."""
        ranked = [(it, s) for it, s in score_titles(taste, self.catalog)]
        score_of = {it.item_id: float(s) for it, s in ranked}

        def j(items: list) -> list:
            return [self._item_json(it) for it in items]

        def row(pool_items: list, key, reverse: bool = True,
                want: int = n) -> list:
            picked: list = []
            seen_ids: set = set()
            for it in sorted(pool_items, key=key, reverse=reverse):
                if it.item_id not in seen_ids:
                    seen_ids.add(it.item_id)
                    picked.append(it)
                if len(picked) >= want:
                    break
            return [self._item_json(x) for x in picked]

        k_taste = lambda it: score_of.get(it.item_id, 0.0)
        k_pop = lambda it: it.popularity
        k_vote = lambda it: it.vote_average
        k_new = lambda it: it.release_date or ""
        k_trend = lambda it: (float(self.genre_plays[self.gidx[it.primary_genre]]),
                              it.popularity)

        def pool(genre: str) -> list:
            return self.by_tag.get(genre, [])

        order = np.argsort(taste)[::-1]
        g1 = self.genres[order[0]]
        trending_idx = np.argsort(self.genre_plays)[::-1][:3]
        trending_genres = {self.genres[i] for i in trending_idx
                           if self.genre_plays[i] > 0}
        tr_pool = [it for it, _ in ranked if it.primary_genre in trending_genres]
        if tr_pool:
            tr_key = k_trend
        else:
            tr_pool = [it for it, _ in ranked]
            tr_key = lambda it: k_taste(it) * it.popularity
        new_cands = [it for it in self.catalog.items
                     if it.release_date >= "2026-08-01"]
        hidden_pool = sorted(self.catalog.items, key=k_pop)[:len(self.catalog.items) // 2]

        rails = [
            ("featured", "Featured", "top picks from your quiz answers",
             [self._item_json(it, s) for it, s in ranked[:10]]),
            ("apps", "Apps", "streaming apps on this TV",
             [{"app": name} for name in APPS]),
            ("personalized", "Personalized for you",
             "ranked live against the quiz-bootstrapped taste vector - 0 watch history",
             [self._item_json(it, s) for it, s in ranked[:n]]),
            ("genre", f"Because of your interest in {genre_pretty(g1)}", "",
             row(pool(g1), k_vote)),
            ("trending", "Trending now", "what the whole simulation is watching today",
             row(tr_pool, tr_key)),
            ("popular", "Popular right now", "highest TMDb popularity today",
             row(self.catalog.items, k_pop)),
            ("new", "New this month", "released in the last 60 days",
             row(new_cands, k_new)),
            ("gems", "Hidden gems for you", "high taste match, low popularity",
             row(hidden_pool, k_vote)),
            ("critics", "Critics' picks", "highest rated of all time",
             row(self.catalog.items, k_vote)),
        ]
        # -- collections registry: drop deprecated/removed collections --
        try:
            cst = self.collections_state
            skip = {c["id"] for c in cst.get("official", [])
                    if c.get("status") == "deprecated"} | set(cst.get("removed", []))
            rails = [r for r in rails if r[0] not in skip]
        except AttributeError:
            pass
        # -- collections circle: fixed 1-2 stay pinned; the rest rotate --
        fixed_keys = {"featured", "apps"}
        fixed_rails = [r for r in rails if r[0] in fixed_keys]
        pool = [r for r in rails if r[0] not in fixed_keys]
        window = 12
        if len(pool) > window:
            start = (self.day * 3) % len(pool)
            rails = fixed_rails + [pool[(start + k) % len(pool)]
                                   for k in range(window)]
        out = []
        for key, t, why, items in rails:
            for d in items:
                if d.get("id"):
                    it = self.by_id.get(d["id"])
                    if it is not None:
                        d["why_hover"] = self._why_hover(None, taste, taste, it)
            out.append({"kind": key, "title": t, "why": why, "items": items})
        return {"rails": out}

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
                    f"Taste match - your {g} weight is {taste[gi]:.2f} and "
                    f"this title is {vec[gi]:.0%} {g}")

        pg = self.genres[int(np.argmax(vec))]
        plays = [e for e in reversed(self.agent_events[persona_id])
                 if e["type"] == "play" and e["item_id"] != item_id
                 and e["item_id"] in self.by_id
                 and pg in self.by_id[e["item_id"]].genre_tags][:3]
        for e in plays:
            signals.append(
                f"You watched “{e['title']}” on day {e['day']} - it's "
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
                    if any(x.get("id") == item_id for x in r["items"])]
        if in_rails:
            extra = f" (+{len(in_rails) - 1} more)" if len(in_rails) > 1 else ""
            signals.append(f"Surfaced in “{in_rails[0]}”{extra}")

        for camp in self.campaigns:
            if pi in camp["targets"] and self._camp_active(camp) and any(
                    vec[self.gidx[g]] > 0.25 for g in camp["genres"]):
                signals.append(
                    f"Master Agent - “{camp['text'][:70]}” is steering "
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

    def _collection_trends(self) -> dict:
        """Per floating-collection engagement trend: % change in attributed
        plays over the last 4 weeks vs the prior 4 weeks. Positive = green,
        negative = red. Agent-agnostic attribution from item properties.
        Recently approved AI collections (within 7 days) show "new" instead
        of a fake baseline."""
        now = self.day
        recent = [e for e in self._watch_log
                  if e.get("type") == "play" and now - 28 <= e["day"] < now]
        prior = [e for e in self._watch_log
                 if e.get("type") == "play" and now - 56 <= e["day"] < now - 28]
        out = {}
        if recent or prior:
            out.update(self._collection_trends_real(recent, prior))
        # Recently approved AI collections show "new" for the first 7 days
        # instead of a seeded baseline - they have no performance history yet.
        try:
            for c in self.collections_state.get("official", []):
                if c.get("ai_approved"):
                    kind = f"ai_{c['id']}"
                    approved = c.get("approved_day", -999)
                    if 0 <= now - approved < 7:
                        out[kind] = "new"
        except AttributeError:
            pass
        # Seeded baselines: stable per collection, varied mix of
        # positive/negative, with a deterministic day-varying drift so the
        # numbers breathe as the user scrolls through days. Real data
        # overrides when available.
        import hashlib
        baselines = [112, 45, -18, 78, -32, 23, 91, -8, 56, -41, 67, 12,
                     -25, 103, 34, -15, 88, -5, 61, -28]
        for kind in self._all_collection_kinds():
            if kind not in out:
                h = int(hashlib.md5(kind.encode()).hexdigest()[:8], 16)
                base = baselines[h % len(baselines)]
                # day drift: deterministic per (kind, day), range -30..+30
                dh = int(hashlib.md5(f"{kind}:{self.day}".encode()).hexdigest()[:8], 16)
                drift = (dh % 61) - 30
                out[kind] = float(max(-99, min(199, base + drift)))
        return out

    def _ai_candidate_title_count(self, cid: str) -> int:
        """Number of catalog titles matching the candidate's theme genres."""
        themes = AI_COLLECTION_THEMES.get(cid)
        if not themes:
            return len(self.catalog.items)
        tset = set(themes)
        return sum(1 for it in self.catalog.items if it.primary_genre in tset)

    def _all_collection_kinds(self) -> list:
        kinds = ["personalized", "genre", "trending", "popular", "new",
                 "gems", "critics", "detour", "because_watched", "lookalike",
                 "marathon", "app", "campaign"]
        try:
            for c in self.collections_state.get("official", []):
                if c.get("ai_approved"):
                    kinds.append(f"ai_{c['id']}")
        except AttributeError:
            pass
        return kinds

    def _collection_trends_real(self, recent, prior) -> dict:
        # item stat thresholds
        pops = sorted(it.popularity for it in self.catalog.items)
        votes = sorted(it.vote_average for it in self.catalog.items)
        pop_q75 = pops[3 * len(pops) // 4]
        vote_q75 = votes[3 * len(votes) // 4]
        pop_med = pops[len(pops) // 2]
        # genre rankings from all plays in window
        def genre_counts(evs):
            c = {}
            for e in evs:
                it = self.by_id.get(e.get("item_id"))
                if it:
                    c[it.primary_genre] = c.get(it.primary_genre, 0) + 1
            return c
        gc = genre_counts(recent + prior)
        top_genres = sorted(gc, key=gc.get, reverse=True)
        top5 = set(top_genres[:5])
        top3 = set(top_genres[:3])
        second = top_genres[1] if len(top_genres) > 1 else None

        def kinds_for(item_id):
            it = self.by_id.get(item_id)
            if not it:
                return set()
            k = set()
            if it.release_date >= "2026-08-01":
                k.add("new")
            if it.popularity >= pop_q75:
                k.add("popular")
            if it.vote_average >= vote_q75:
                k.add("critics")
            if it.popularity < pop_med and it.vote_average >= vote_q75:
                k.add("gems")
            if it.primary_genre not in top5:
                k.add("detour")
            if it.primary_genre in top3:
                k.add("trending")
                k.add("genre")
            if second and it.primary_genre == second:
                k.add("marathon")
            # personalized / because_watched / lookalike / app / campaign
            # are agent-specific; attribute broadly to personalized
            k.add("personalized")
            return k

        def count(evs):
            c = {}
            for e in evs:
                for k in kinds_for(e.get("item_id")):
                    c[k] = c.get(k, 0) + 1
            return c
        cr, cp = count(recent), count(prior)
        out = {}
        for k in set(cr) | set(cp):
            r, p = cr.get(k, 0), cp.get(k, 0)
            # Require a real baseline: without enough prior plays the
            # % change is degenerate (p=0 always yields exactly +100%).
            # Fall back to seeded baselines until history accumulates.
            if p < 5:
                continue
            pct = (r - p) / p * 100.0
            out[k] = round(max(-999.0, min(999.0, pct)), 1)
        return out

    def _why_hover(self, pi: int | None, taste, rank_taste, item) -> str:
        """One compact hover explanation per tile: taste match %, any live
        Master Agent boost, and today's trending velocity. Purely additive
        display data - never changes ranking."""
        vec = np.asarray(item.genre_vector, dtype=float)
        lines = ["Why this?",
                 f"Taste match {100 * float(np.dot(taste, vec)):.0f}%"]
        if pi is not None:
            for camp in self.campaigns:
                if pi in camp["targets"] and self._camp_active(camp) and any(
                        vec[self.gidx[g]] > 0.25 for g in camp["genres"]):
                    base = float(np.dot(taste, vec))
                    boosted = float(np.dot(rank_taste, vec))
                    lift = (boosted - base) / base if base > 1e-9 else 0.0
                    glabel = ("Baseball" if camp["genres"][0] == "Sports"
                              else genre_pretty(camp["genres"][0]))
                    lines.append(f"Master Agent boost +{lift:.0%} ({glabel} push)")
                    break
        gi = self.gidx[item.primary_genre]
        plays = float(self.genre_plays[gi])
        if plays > 0:
            rank = int((self.genre_plays > plays).sum()) + 1
            if rank <= 5:
                lines.append(f"Trending #{rank} today ({int(plays)} plays)")
        return "\n".join(lines)

    @staticmethod
    def _norm_text(s: str) -> str:
        return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", s.lower())).strip()

    @classmethod
    def _query_matches_title(cls, qn: str, title: str) -> bool:
        """Query-aware boost: does the normalized query relate to the title?"""
        if not qn:
            return False
        tn = cls._norm_text(title)
        if qn in tn or tn in qn:
            return True
        qwords = {w for w in qn.split() if len(w) > 2}
        twords = set(tn.split())
        return bool(qwords & twords)

    def search(self, q: str, persona_id: str | None = None, n: int = 12) -> list[dict]:
        ql = q.lower().strip()
        # punctuation-insensitive query: "avengers doomsday" matches
        # "Avengers: Doomsday"
        qn = self._norm_text(ql)
        taste = None
        pidx = None
        if persona_id:
            p = next((pp for pp in self.personas if pp.persona_id == persona_id), None)
            if p is not None:
                pidx = self.personas.index(p)
                taste = self.tastes[pidx]
        scored = []
        for it in self.catalog.items:
            hay = f"{it.title} {it.primary_genre}".lower()
            hayn = self._norm_text(hay)
            if ql and ql not in hay and (not qn or qn not in hayn):
                continue
            s = float(np.dot(taste, it.genre_vector)) if taste is not None else 0.0
            # prefix / title matches rank first (normalized)
            tn = self._norm_text(it.title)
            boost = 2.0 if tn.startswith(qn) else (1.0 if qn and qn in tn else 0.0)
            scored.append((boost, s, it))
        scored.sort(key=lambda t: (-t[0], -t[1], t[2].item_id))
        # Paid search slot: exactly one. While a provider_promo campaign with
        # the "search" placement is live, any query matching one of the
        # provider's exclusive titles puts the best-matching title in the
        # paid slot, tagged "Promoted" instead of "Sponsored". Shown to the
        # campaign's targeted agents (or when no agent is viewing); queries
        # with no provider match keep the generic Sponsored pick. The paid
        # title is deterministic per query; only the slot is randomized
        # (never first).
        paid_item, paid_score, paid_promoted = None, 0.0, False
        paid_camp = None
        if ql:
            for camp in self.campaigns:
                if (camp.get("campaign_type") != "provider_promo"
                        or not self._camp_active(camp)):
                    continue
                if pidx is not None and pidx not in camp.get("targets", ()):
                    continue
                if "search" not in camp.get("placements", ["collection"]):
                    continue
                provider = camp.get("provider")
                matches = []
                for tid in camp.get("titles", ()):
                    it = self._resolve_item(tid)
                    if it is None:
                        continue
                    tnorm = self._norm_text(it.title)
                    if qn not in tnorm:
                        continue
                    # original provider titles only: exclusive to the app
                    if tuple(it.providers) != (provider,):
                        continue
                    prefix = tnorm.startswith(qn)
                    matches.append((prefix, it.popularity, it))
                if matches:
                    matches.sort(key=lambda t: (-t[0], -t[1]))
                    paid_item = matches[0][2]
                    paid_camp = camp
                    paid_promoted = True
                    break
        if paid_item is None and scored:
            spon = max(scored, key=lambda t: t[2].popularity)
            paid_item, paid_score = spon[2], spon[1]
        # Search boost campaigns: pinned titles lead the results for targeted
        # agents, tagged Promoted. Query-aware: the boost only fires when the
        # search text relates to the title (substring or shared word).
        boosted = []
        if ql:
            for camp in self.campaigns:
                if (camp.get("campaign_type") != "search_boost"
                        or not self._camp_active(camp)):
                    continue
                if pidx is not None and pidx not in camp.get("targets", ()):
                    continue
                for tid in sorted(camp.get("titles", ())):
                    it = self.by_id.get(tid)
                    if it is None:
                        continue
                    if not self._query_matches_title(qn, it.title):
                        continue
                    d = self._item_json(it)
                    d["promoted"] = True
                    boosted.append(d)
                if pidx is not None:
                    camp["impressions"].add((pidx, self.day))
        out = []
        if paid_item is not None:
            d = self._item_json(paid_item, paid_score)
            d["promoted"] = paid_promoted
            d["sponsored"] = not paid_promoted
            rest = [t for t in scored if t[2] is not paid_item]
            organics = [self._item_json(it, s) for _, s, it in rest[:n - 1]]
            if organics:
                pos = random.randint(1, min(3, len(organics)))
                organics.insert(pos, d)
                out = organics[:n]
            else:
                out = [d]
            if paid_camp is not None and pidx is not None:
                # A search view of the promo counts as a same-day impression,
                # like the rail view does, so conversions stay attributable.
                paid_camp["impressions"].add((pidx, self.day))
        # boosted titles lead; dedup against the rest
        seen_ids = set()
        final = []
        for d in boosted + out:
            if d["id"] not in seen_ids:
                seen_ids.add(d["id"])
                final.append(d)
        return final[:n]

    def trending_searches(self, n: int = 8) -> dict:
        """Trending searches for the empty-search state: content cards for
        the top recent queries (real frontend + simulated agent searches),
        with boosted titles from active search_boost campaigns leading the
        list tagged Promoted. Backfilled with popular genres so the row is
        never empty."""
        from collections import Counter
        counts: Counter = Counter()
        cutoff = self.day - 7
        for e in self._query_log:
            if e["day"] >= cutoff and e["q"]:
                counts[e["q"]] += 1
        for e in self._watch_log:
            if e.get("type") == "search" and e.get("day", 0) >= cutoff:
                q = e.get("query")
                if q:
                    counts[q] += 1
        queries = [q for q, _ in counts.most_common(n)]
        # Backfill with popular genres so there's always a full collection.
        if len(queries) < n:
            gcounts: Counter = Counter()
            for it in self.catalog.items:
                gcounts[it.primary_genre] += it.popularity
            seen_q = {q.lower() for q in queries}
            for g, _ in gcounts.most_common():
                if g.lower() not in seen_q:
                    queries.append(g)
                    seen_q.add(g.lower())
                if len(queries) >= n:
                    break
        # Resolve each query to a content card.
        genre_names = {g.lower(): g for g in self.genres}
        items = []
        seen_ids = set()

        def top_for_genre(gname):
            cands = [it for it in self.catalog.items
                     if it.primary_genre == gname]
            if not cands:
                return None
            return max(cands, key=lambda it: it.popularity)

        # Promoted: boosted titles lead, tagged Promoted.
        for camp in self.campaigns:
            if (camp.get("campaign_type") != "search_boost"
                    or not self._camp_active(camp)):
                continue
            for tid in sorted(camp.get("titles", ())):
                it = self.by_id.get(tid)
                if it is None or it.item_id in seen_ids:
                    continue
                seen_ids.add(it.item_id)
                d = self._item_json(it)
                d["promoted"] = True
                items.append(d)
        # Organic: top card per trending query.
        for q in queries:
            gl = q.lower()
            if gl in genre_names:
                it = top_for_genre(genre_names[gl])
            else:
                res = self.search(q, n=1)
                it = self.by_id.get(res[0]["id"]) if res else None
            if it is None or it.item_id in seen_ids:
                continue
            seen_ids.add(it.item_id)
            items.append(self._item_json(it))
            if len(items) >= n + 4:
                break
        return {"items": items[:n + 4]}

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
        # Subscription-promotion directives ("run a FoxOne promotion to those
        # who don't have it"): matched before the genre scan, since promo
        # wording carries no genre keyword and would return "unknown".
        promo_app = None
        for kw, app in PROVIDER_PROMO_KEYWORDS.items():
            if re.search(r"\b" + re.escape(kw) + r"\b", t):
                promo_app = app
                break
        # Elections directives ("pivot all users to elections related content
        # for 14 days"): matched before the genre scan like promos, since
        # "elections" is a content theme, not a Gracenote genre.
        elections = promo_app is None and re.search(
            r"\b(elections?|midterms?|voting)\b", t) is not None
        genres: list[str] = []
        if promo_app is None and not elections:
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
        if promo_app is not None:
            # Provider promo: reuse the coverage/cluster/days/scheduling
            # parsing above. No genre pivot - a paid placement never steers
            # taste, so genres stays empty and vec/weight stay neutral.
            return {"action": "start", "campaign_type": "provider_promo",
                    "provider": promo_app, "genres": [],
                    "coverage": coverage, "cluster": cluster,
                    "days": days, "start_day": start_day}
        if elections:
            # Elections coverage: title-pinned like a promo, not a taste
            # pivot. Genres stays empty; the rail and live tile carry it.
            return {"action": "start", "campaign_type": "elections",
                    "genres": [],
                    "coverage": coverage, "cluster": cluster,
                    "days": days, "start_day": start_day}
        return {"action": "start", "genres": genres, "coverage": coverage,
                "cluster": cluster, "days": days, "start_day": start_day}

    def _camp_active(self, c: dict) -> bool:
        """A campaign only steers the sim once its start day arrives."""
        return c["start_day"] <= self.day

    def _campaign_target_labels(self, c: dict) -> list:
        """Human labels for a campaign's per-agent target badges.

        provider_promo campaigns store genres=[] (they steer titles, not
        tastes), so their targets get the provider name instead of genres.
        """
        ct = c.get("campaign_type")
        if ct == "provider_promo":
            return [c.get("provider") or "promotion"]
        if ct == "featured":
            return ["featured row"]
        if ct == "search_boost":
            return ["search boost"]
        if ct == "takeover":
            return ["takeover"]
        if ct == "elections":
            return ["elections coverage"]
        return list(c.get("genres") or [])

    def _campaigns_json(self) -> list[dict]:
        return [{"id": c["id"], "text": c["text"], "genres": c["genres"],
                 "campaign_type": c.get("campaign_type", "genre_pivot"),
                 "provider": c.get("provider"),
                 "titles": sorted(c.get("titles") or ()),
                 "title_id": c.get("title_id"), "headline": c.get("headline", ""),
                 "n_targets": len(c["targets"]), "targets": sorted(c["targets"]),
                 "weight": c["weight"],
                 "days_left": c["days_left"], "days_total": c["days_total"],
                 "created_day": c["created_day"], "start_day": c["start_day"],
                 "placements": c.get("placements", []),
                 "status": "live" if self._camp_active(c) else "scheduled"}
                for c in self.campaigns]

    def _campaign_taste_shift(self, targets: list[int], genres: list[str],
                              base_end: int, win_end: int) -> float | None:
        """Average taste-weight change (percentage points) on the campaign
        genres between the baseline and the live window, read from the
        stored daily taste snapshots."""
        gi = [self.gidx[g] for g in genres if g in self.gidx]
        if not targets or not gi:
            return None

        def avg_at(day: int) -> float | None:
            snaps = [(d, t) for d, t in self._all_paths if d <= day]
            if not snaps:
                return None
            t = snaps[-1][1]
            return float(t[targets][:, gi].sum(axis=1).mean())

        before, after = avg_at(base_end), avg_at(win_end)
        if before is None or after is None:
            return None
        return round(100 * (after - before), 2)

    def _promo_analytics(self, c: dict, src: str) -> dict:
        """Closed-loop metrics for a subscription-promotion campaign: promo
        rail impressions, clicks on the pinned titles, and subscription
        conversions - counted from the sim's own event log over the live
        window. Genre plays/lift/taste-shift are meaningless for a paid
        placement and stay null/zero."""
        targets = [i for i in (c.get("targets") or [])
                   if 0 <= i < len(self.personas)]
        titles = set(c.get("titles") or ())
        days_total = max(int(c.get("days_total") or 1), 1)
        start = int(c.get("start_day", c.get("created_day", 0)))
        # tick() stamps events with day+1, so the live window in event-day
        # terms starts one day after start_day - same convention as the genre
        # branch.
        win_start = start + 1
        win_end = min(self.day, start + days_total)
        win_days = set(range(win_start, win_end + 1)) if win_end >= win_start else set()
        # impressions: unique (agent, day) rail views inside the window
        impressions = sum(1 for (_i, d) in (c.get("impressions") or set())
                          if d in win_days)
        clicks = conversions = 0
        for i in targets:
            pid = self.personas[i].persona_id
            for e in self.agent_events.get(pid, []):
                if not (win_start <= e.get("day", 0) <= win_end):
                    continue
                t = e.get("type")
                if t == "click" and e.get("item_id") in titles:
                    clicks += 1
                elif (t == "conversion"
                      and e.get("campaign_id") == c.get("id")):
                    conversions += 1
        if src == "live":
            status = "live" if self._camp_active(c) else "scheduled"
        else:
            status = "ended"
        return {
            "id": c.get("id"), "text": c.get("text", ""),
            "campaign_type": "provider_promo", "provider": c.get("provider"),
            "genres": [], "status": status,
            "n_targets": len(targets),
            "days_total": days_total, "start_day": start,
            "plays_live": 0, "plays_baseline": 0,
            "lift": None, "taste_shift_pp": None,
            "impressions": impressions, "clicks": clicks,
            "conversions": conversions,
            "conversion_rate": (round(conversions / impressions, 4)
                                if impressions else None),
        }

    def takeover_roi(self) -> list[dict]:
        """Sponsor ROI for takeover campaigns: views, watch clicks,
        dismissals, and watch-through from the sim's event log."""
        out = []
        for src, cs in (("live", self.campaigns), ("ended", self.campaign_history)):
            for c in cs:
                if c.get("campaign_type") != "takeover":
                    continue
                views = len(c.get("takeover_views") or set())
                dismissals = len(c.get("takeover_dismissals") or set())
                title_id = c.get("title_id", "")
                # watch clicks: plays of the takeover title by targets
                # during the live window
                days_total = max(int(c.get("days_total") or 1), 1)
                start = int(c.get("start_day", c.get("created_day", 0)))
                win_start = start + 1
                win_end = min(self.day, start + days_total)
                targets = set(c.get("targets") or [])
                watches = 0
                for i in targets:
                    if 0 <= i < len(self.personas):
                        pid = self.personas[i].persona_id
                        for e in self.agent_events.get(pid, []):
                            if (e.get("type") == "play"
                                    and e.get("item_id") == title_id
                                    and win_start <= e.get("day", 0) <= win_end):
                                watches += 1
                                break
                ctr = round(watches / views, 4) if views else None
                dismiss_rate = round(dismissals / views, 4) if views else None
                out.append({
                    "id": c.get("id"),
                    "sponsor": c.get("sponsor", ""),
                    "title_id": title_id,
                    "title": (self.by_id[title_id].title
                              if title_id in self.by_id else ""),
                    "headline": c.get("headline", ""),
                    "status": ("live" if self._camp_active(c) else
                               "scheduled" if src == "live" else "ended"),
                    "n_targets": len(targets),
                    "views": views,
                    "watches": watches,
                    "dismissals": dismissals,
                    "ctr": ctr,
                    "dismiss_rate": dismiss_rate,
                    "days_total": days_total,
                    "start_day": start,
                })
        return out

    def campaign_analytics(self) -> list[dict]:
        """Closed-loop campaign metrics from the sim's own event log: plays
        in the campaign genres during the live window vs. the equal-length
        baseline before it, taste shift of the targets on those genres, and
        clicks. Covers live, scheduled, and ended campaigns."""
        out = []
        for src, cs in (("live", self.campaigns), ("ended", self.campaign_history)):
            for c in cs:
                if c.get("campaign_type") == "provider_promo":
                    out.append(self._promo_analytics(c, src))
                    continue
                targets = [i for i in (c.get("targets") or [])
                           if 0 <= i < len(self.personas)]
                genres = list(c.get("genres") or [])
                gset = set(genres)
                days_total = max(int(c.get("days_total") or 1), 1)
                start = int(c.get("start_day", c.get("created_day", 0)))
                # tick() stamps events with day+1, so the live window in
                # event-day terms starts one day after start_day.
                win_start = start + 1
                win_end = min(self.day, start + days_total)
                base_start, base_end = win_start - days_total, win_start - 1
                live_plays = base_plays = clicks = 0
                if targets and gset and win_end >= win_start:
                    for i in targets:
                        pid = self.personas[i].persona_id
                        for e in self.agent_events.get(pid, []):
                            if e.get("genre") not in gset:
                                continue
                            d = e.get("day", 0)
                            t = e.get("type")
                            if t == "play":
                                if win_start <= d <= win_end:
                                    live_plays += 1
                                elif base_start <= d <= base_end:
                                    base_plays += 1
                            elif t == "click" and win_start <= d <= win_end:
                                clicks += 1
                lift = ((live_plays - base_plays) / base_plays
                        if base_plays > 0 else None)
                if src == "live":
                    status = ("live" if self._camp_active(c)
                              else "scheduled")
                else:
                    status = "ended"
                out.append({
                    "id": c.get("id"), "text": c.get("text", ""),
                    "genres": genres, "status": status,
                    "n_targets": len(targets),
                    "days_total": days_total, "start_day": start,
                    "plays_live": live_plays, "plays_baseline": base_plays,
                    "lift": round(lift, 3) if lift is not None else None,
                    "taste_shift_pp": self._campaign_taste_shift(
                        targets, genres, base_end, win_end),
                    "clicks": clicks,
                })
        return out

    def stop_campaign(self, campaign_id: int) -> dict:
        """Stop one campaign by id; move it to history like 'stop campaigns'."""
        for i, c in enumerate(self.campaigns):
            if c["id"] == campaign_id:
                self.campaign_history.append({
                    "id": c["id"], "text": c["text"], "genres": c["genres"],
                    "campaign_type": c.get("campaign_type", "genre_pivot"),
                    "provider": c.get("provider"),
                    "titles": sorted(c.get("titles") or []),
                    "n_targets": len(c["targets"]),
                    "targets": sorted(c["targets"]),
                    "created_day": c["created_day"],
                    "days_total": c["days_total"], "ended_day": self.day,
                    "live": False})
                del self.campaigns[i]
                self._truncate_future()
                return {"ok": True, "message": f"Stopped campaign {campaign_id}."}
        return {"ok": False, "message": f"No live campaign {campaign_id}."}

    def direct(self, text: str) -> dict:
        """The manager agent: turn a plain-English directive into a campaign."""
        self._truncate_future()  # a new directive starts a new timeline branch
        parsed = self.parse_directive(text)
        if parsed["action"] == "stop":
            for c in self.campaigns:
                self.campaign_history.append({
                    "id": c["id"], "text": c["text"], "genres": c["genres"],
                    "campaign_type": c.get("campaign_type", "genre_pivot"),
                    "provider": c.get("provider"),
                    "titles": sorted(c.get("titles") or []),
                    "n_targets": len(c["targets"]),
                    "targets": sorted(c["targets"]),
                    "created_day": c["created_day"],
                    "days_total": c["days_total"], "ended_day": self.day,
                    "live": False})
            n = len(self.campaigns)
            self.campaigns.clear()
            msg = f"Master Agent stopped {n} campaign(s)."
        elif parsed["action"] == "unknown":
            msg = ("Master Agent didn't catch that - try “pivot some users to "
                   "football for 7 days” or “stop campaigns”.")
        else:
            self._camp_seq += 1
            promo = parsed.get("campaign_type") == "provider_promo"
            is_elections = parsed.get("campaign_type") == "elections"
            if promo:
                # Subscription promotion: acquisition targeting - only agents
                # that don't already subscribe. Conversions mutate
                # subscribed_apps, so converted agents become ineligible for
                # later promo runs automatically. Zero eligible: the campaign
                # still goes live with empty targets (analytics handles it).
                provider = parsed["provider"]
                eligible = [i for i, p in enumerate(self.personas)
                            if provider not in p.subscribed_apps]
                if parsed["cluster"] is not None:
                    targets = {i for i in eligible
                               if int(self.labels[i]) == parsed["cluster"]}
                elif eligible:
                    k = max(1, int(parsed["coverage"] * len(eligible)))
                    targets = set(self.rng.choice(
                        eligible, min(k, len(eligible)),
                        replace=False).tolist())
                else:
                    targets = set()
                # Pinned titles: catalog items streaming on the provider,
                # resolved through the real provider->app mapping (NOT the
                # mock hash pool). A paid placement never steers taste:
                # vec None, weight 0.0.
                titles = self._provider_titles(provider)
                camp = {"id": self._camp_seq, "text": text,
                        "campaign_type": "provider_promo",
                        "provider": provider,
                        "genres": [], "targets": targets,
                        "titles": titles, "impressions": set(),
                        "vec": None, "weight": 0.0,
                        "placements": ["collection", "search", "featured"],
                        "days_left": parsed["days"],
                        "days_total": parsed["days"],
                        "created_day": self.day,
                        "start_day": parsed["start_day"]}
            elif is_elections:
                # Elections coverage: all-agents (or parsed coverage) title
                # push. Pins election titles; the rail + live tile carry it.
                # No taste steering - vec None, weight 0.0.
                if parsed["cluster"] is not None:
                    targets = {i for i in range(self.n_agents)
                               if int(self.labels[i]) == parsed["cluster"]}
                else:
                    k = max(1, int(parsed["coverage"] * self.n_agents))
                    targets = set(self.rng.choice(
                        self.n_agents, k, replace=False).tolist())
                titles = self._election_titles()
                camp = {"id": self._camp_seq, "text": text,
                        "campaign_type": "elections",
                        "genres": [], "targets": targets,
                        "titles": titles, "impressions": set(),
                        "vec": None, "weight": 0.0,
                        "days_left": parsed["days"],
                        "days_total": parsed["days"],
                        "created_day": self.day,
                        "start_day": parsed["start_day"]}
            else:
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
                        "days_left": parsed["days"],
                        "days_total": parsed["days"],
                        "created_day": self.day,
                        "start_day": parsed["start_day"]}
            self.campaigns.append(camp)
            who = (f"cluster {parsed['cluster']}" if parsed["cluster"] is not None
                   else f"{len(targets)} agents")
            if parsed["start_day"] <= self.day:
                if promo:
                    msg = (f"Master Agent: running a {parsed['provider']} "
                           f"subscription promotion for {who} - "
                           f"{parsed['days']} days. "
                           f"⏸ Sim paused so you can inspect - hit ▶ Play to watch it run.")
                elif is_elections:
                    msg = (f"Master Agent: running elections coverage for {who} - "
                           f"{parsed['days']} days. "
                           f"⏸ Sim paused so you can inspect - hit ▶ Play to watch it run.")
                else:
                    msg = (f"Master Agent: pivoting {who} toward "
                           f"{', '.join(parsed['genres'])} for {parsed['days']} days. "
                           f"⏸ Sim paused so you can inspect - hit ▶ Play to watch it fade.")
                camp["announced"] = True
                # Freeze the sim the moment a campaign goes live: at the
                # default tick a 7-day campaign evaporates in ~11 real seconds,
                # which makes every visual vanish before it can be inspected.
                self.running = False
            else:
                end = parsed["start_day"] + parsed["days"] - 1
                if promo:
                    msg = (f"Master Agent: scheduled a {parsed['provider']} "
                           f"subscription promotion for {who} - days "
                           f"{parsed['start_day']}–{end}. "
                           f"Nothing changes on screen until day {parsed['start_day']}.")
                elif is_elections:
                    msg = (f"Master Agent: scheduled elections coverage for {who} - days "
                           f"{parsed['start_day']}–{end}. "
                           f"Nothing changes on screen until day {parsed['start_day']}.")
                else:
                    glabels = [("Baseball" if g == "Sports" else genre_pretty(g))
                               for g in parsed["genres"]]
                    msg = (f"Master Agent: scheduled {', '.join(glabels)} "
                           f"for {who} - days {parsed['start_day']}–{end}. "
                           f"Nothing changes on screen until day {parsed['start_day']}.")
                camp["announced"] = False
        self.master_log.append({"day": self.day, "text": msg})
        self.master_log = self.master_log[-30:]
        self._master_log_all.append({"day": self.day, "text": msg})
        if len(self._master_log_all) > 1000:
            del self._master_log_all[:500]
        n_targets = len(targets) if parsed["action"] == "start" else 0
        ev_targets = sorted(targets) if parsed["action"] == "start" else []
        # scheduled campaigns announce quietly - the energy blast fires when
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

    # -- structured campaign launchers (Master Agent sections) ----------------
    def _pick_targets(self, coverage: float, cluster: int | None,
                      eligible=None) -> set:
        """Target agent set from coverage/cluster, mirroring direct()."""
        pool = list(eligible) if eligible is not None else list(range(self.n_agents))
        if cluster is not None:
            return {i for i in pool if int(self.labels[i]) == cluster}
        if not pool:
            return set()
        k = max(1, int(max(0.0, min(1.0, coverage)) * len(pool)))
        return set(self.rng.choice(pool, min(k, len(pool)), replace=False).tolist())

    def _register_campaign(self, camp: dict, msg: str) -> dict:
        """Shared launch tail: log, events, sim pause, snapshot."""
        self._truncate_future()
        # provider_promo campaigns from the natural-language directive path
        # carry no explicit placements - keep the legacy all-surfaces behavior.
        if (camp.get("campaign_type") == "provider_promo"
                and not camp.get("placements")):
            camp["placements"] = ["collection", "search", "featured"]
        self.campaigns.append(camp)
        if camp["start_day"] <= self.day:
            msg = msg + " ⏸ Sim paused so you can inspect - hit ▶ Play to watch it run."
            camp["announced"] = True
            self.running = False
        else:
            camp["announced"] = False
        self.master_log.append({"day": self.day, "text": msg})
        self.master_log = self.master_log[-30:]
        self._master_log_all.append({"day": self.day, "text": msg})
        if len(self._master_log_all) > 1000:
            del self._master_log_all[:500]
        scheduled = camp["start_day"] > self.day
        ev = {"kind": "directed", "day": self.day, "text": msg,
              "n_targets": 0 if scheduled else len(camp["targets"]),
              "targets": [] if scheduled else sorted(camp["targets"]),
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
        self._store_snapshot()
        return {"ok": True, "message": msg,
                "campaigns": self._campaigns_json(),
                "log": self.master_log[-10:]}

    def _new_campaign_shell(self, campaign_type: str, targets: set,
                            days: int, start_day: int, text: str) -> dict:
        self._camp_seq += 1
        return {"id": self._camp_seq, "text": text,
                "campaign_type": campaign_type,
                "genres": [], "targets": targets,
                "titles": set(), "impressions": set(),
                "takeover_views": set(), "takeover_dismissals": set(),
                "provider": None, "headline": "",
                "vec": None, "weight": 0.0,
                "days_left": days, "days_total": days,
                "created_day": self.day, "start_day": start_day}

    def start_app_campaign(self, provider: str, coverage: float = 0.25,
                           cluster: int | None = None, days: int = 7,
                           start_day: int | None = None,
                           placements: list | None = None) -> dict:
        """Apps campaign: subscription push for any farm app (provider_promo).

        placements picks which surfaces promote the app: "collection"
        (exclusive-only provider rail), "search" (Promoted search slot),
        "featured" (promoted tile at a random spot in the Featured row).
        """
        if provider not in APPS:
            return {"ok": False, "message": f"Unknown app: {provider}"}
        sd = self.day if start_day is None else max(start_day, self.day)
        eligible = [i for i, p in enumerate(self.personas)
                    if provider not in p.subscribed_apps]
        targets = self._pick_targets(coverage, cluster, eligible)
        titles = self._provider_titles(provider)
        camp = self._new_campaign_shell(
            "provider_promo", targets, days, sd,
            f"Master Agent: {provider} subscription push")
        valid = {"collection", "search", "featured"}
        placements = [p for p in (placements or ["collection"]) if p in valid] or ["collection"]
        camp.update({"provider": provider, "titles": titles,
                     "placements": placements})
        who = (f"cluster {cluster}" if cluster is not None
               else f"{len(targets)} agents")
        return self._register_campaign(
            camp, f"Master Agent: running a {provider} subscription "
                  f"promotion for {who} - {days} days.")

    def start_featured_campaign(self, title_ids: list[str], coverage: float = 0.25,
                                cluster: int | None = None, days: int = 7,
                                start_day: int | None = None) -> dict:
        """Featured row campaign: pin titles into the Featured rail."""
        sd = self.day if start_day is None else max(start_day, self.day)
        tids = {t for t in title_ids if t in self.by_id}
        if not tids:
            return {"ok": False, "message": "No valid titles selected."}
        targets = self._pick_targets(coverage, cluster)
        camp = self._new_campaign_shell(
            "featured", targets, days, sd,
            "Master Agent: featured row takeover")
        camp["titles"] = tids
        who = (f"cluster {cluster}" if cluster is not None
               else f"{len(targets)} agents")
        return self._register_campaign(
            camp, f"Master Agent: featuring {len(tids)} title(s) in the "
                  f"Featured row for {who} - {days} days.")

    def start_search_campaign(self, title_ids: list[str], coverage: float = 0.25,
                              cluster: int | None = None, days: int = 7,
                              start_day: int | None = None) -> dict:
        """Search results campaign: pin titles atop search for targets.
        Boosted titles also appear as Promoted terms in trending searches."""
        sd = self.day if start_day is None else max(start_day, self.day)
        tids = {t for t in title_ids if t in self.by_id}
        if not tids:
            return {"ok": False, "message": "No valid titles selected."}
        targets = self._pick_targets(coverage, cluster)
        camp = self._new_campaign_shell(
            "search_boost", targets, days, sd,
            "Master Agent: search results boost")
        camp["titles"] = tids
        who = (f"cluster {cluster}" if cluster is not None
               else f"{len(targets)} agents")
        return self._register_campaign(
            camp, f"Master Agent: boosting {len(tids)} title(s) in search "
                  f"results for {who} - {days} days.")

    def start_takeover_campaign(self, title_id: str, headline: str,
                                coverage: float = 0.25,
                                cluster: int | None = None, days: int = 7,
                                start_day: int | None = None,
                                sponsor: str = "") -> dict:
        """Screen takeover campaign: full-screen promo for targets.
        Optional sponsor adds branded overlay (e.g. Coca-Cola)."""
        sd = self.day if start_day is None else max(start_day, self.day)
        if title_id not in self.by_id:
            return {"ok": False, "message": "No valid title selected."}
        targets = self._pick_targets(coverage, cluster)
        camp = self._new_campaign_shell(
            "takeover", targets, days, sd,
            "Master Agent: screen takeover")
        camp["title_id"] = title_id
        camp["headline"] = headline.strip()[:140]
        camp["sponsor"] = sponsor.strip()[:60]
        who = (f"cluster {cluster}" if cluster is not None
               else f"{len(targets)} agents")
        return self._register_campaign(
            camp, f"Master Agent: screen takeover for {who} - {days} days.")

    def recommendation(self) -> dict:
        """The Master Agent's recommended campaign card: a Fox One
        subscription push. Launchable while no live FOX One provider_promo
        campaign exists; once one is live the card points at it instead."""
        directive = ("run FoxOne promotion to those who don't have active "
                     "subscription of it")
        live = next((c for c in self.campaigns
                     if c.get("campaign_type") == "provider_promo"
                     and c.get("provider") == "FOX One"
                     and self._camp_active(c)), None)
        base = {"id": "foxone_promo",
                "title": "Fox One subscription push",
                "description": ("Targets agents without an active Fox One "
                                "subscription, shows them a \u201cTrending on "
                                "FoxOne\u201d promoted rail, and tracks "
                                "impressions, clicks and subscription "
                                "conversions."),
                "directive": directive}
        if live is None:
            return {**base, "launchable": True, "live": None}
        return {**base, "launchable": False,
                "live": {"id": live["id"],
                         "n_targets": len(live["targets"]),
                         "days_left": live["days_left"],
                         "start_day": live["start_day"]}}


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
            if route == "/VERSION":
                return self._static("VERSION", "text/plain; charset=utf-8")
            if route.startswith("/logos/"):
                name = route[len("/logos/"):]
                # logos only: no path traversal, svg/png only
                if "/" in name or "\\" in name or not name.endswith((".svg", ".png", ".webp")):
                    return self._error(404, "not found")
                ctype = ("image/svg+xml" if name.endswith(".svg")
                         else "image/webp" if name.endswith(".webp")
                         else "image/png")
                return self._static("logos/" + name, ctype)
            if route == "/api/apps":
                with sim.lock:
                    return self._json({"apps": list(APPS)})
            if route == "/api/tentpoles":
                return self._json({"tentpoles": [
                    {"name": n, "date": d, "blurb": b}
                    for n, d, b in US_TENTPOLES]})
            if route == "/api/rail_config":
                with sim.lock:
                    labels = dict(RAIL_LABELS)
                    try:
                        for c in sim.collections_state.get("official", []):
                            if c.get("ai_approved") and c.get("status") == "active":
                                labels[f"ai_{c['id']}"] = c["title"]
                    except AttributeError:
                        pass
                    return self._json({"rails": [
                        {"kind": k, "title": t} for k, t in labels.items()],
                        "hidden": list(sim.rail_config.get("hidden", [])),
                        "order": list(sim.rail_config.get("order", [])),
                        "pinned": dict(sim.rail_config.get("pinned", {})),
                        "trends": sim._collection_trends()})
            if route == "/api/collections":
                with sim.lock:
                    st = sim.collections_state
                    cands = []
                    for c in st.get("ai_candidates", []):
                        cc = dict(c)
                        cc["title_count"] = sim._ai_candidate_title_count(c["id"])
                        cands.append(cc)
                    return self._json({
                        "fixed": [c for c in st["official"] if c.get("fixed")],
                        "official": [c for c in st["official"] if not c.get("fixed")],
                        "removed": st.get("removed", []),
                        "ai_candidates": cands,
                    })
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
                        "recommendation": sim.recommendation(),
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
                            "sports_pivot": pi == sim.sports_pivot_idx,
                            # hand-pinned taste anchor, e.g. "Sports"
                            "pin": ANCHOR_PINS.get(p.persona_id),
                            # live campaign genres (or provider names for promos)
                            # hitting this agent, if any
                            "targeted": sorted({g for c in sim.campaigns
                                                for g in sim._campaign_target_labels(c)
                                                if pi in c["targets"]
                                                and sim._camp_active(c)}),
                            # scheduled-but-not-yet-live campaign labels
                            "scheduled": sorted({g for c in sim.campaigns
                                                 for g in sim._campaign_target_labels(c)
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
                    if q.strip():
                        sim._query_log.append({"day": sim.day, "q": q.strip()})
                        if len(sim._query_log) > 2000:
                            del sim._query_log[:500]
                    return self._json({"results": sim.search(q, pid)})
            if route == "/api/trending_searches":
                with sim.lock:
                    return self._json(sim.trending_searches())
            if route == "/api/explain":
                pid = qs.get("agent", [None])[0]
                iid = qs.get("item", [None])[0]
                with sim.lock:
                    return self._json(sim.explain(pid, iid))
            if route == "/api/campaign_analytics":
                with sim.lock:
                    return self._json({"analytics": sim.campaign_analytics()})
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
            if url.path == "/api/stop_campaign":
                with sim.lock:
                    r = sim.stop_campaign(int(data.get("id", 0) or 0))
                    r["campaigns"] = sim._campaigns_json()
                    return self._json(r)
            if url.path == "/api/rail_config/update":
                valid = set(RAIL_LABELS)
                try:
                    for c in sim.collections_state.get("official", []):
                        if c.get("ai_approved") and c.get("status") == "active":
                            valid.add(f"ai_{c['id']}")
                except AttributeError:
                    pass
                hidden = [k for k in data.get("hidden", []) if k in valid]
                order = [k for k in data.get("order", []) if k in valid]
                pinned = {}
                for k, v in (data.get("pinned", {}) or {}).items():
                    try:
                        pos = int(v)
                    except (TypeError, ValueError):
                        continue
                    if k in valid and pos >= 4:
                        pinned[k] = pos
                sim.rail_config["pinned"] = pinned
                with sim.lock:
                    sim.rail_config = {"hidden": hidden, "order": order,
                                       "pinned": pinned}
                    sim._store_snapshot()
                    return self._json({"ok": True,
                                       "hidden": hidden, "order": order,
                                       "pinned": pinned})
            if url.path == "/api/collections/deprecate":
                cid = data.get("id", "")
                with sim.lock:
                    st = sim.collections_state
                    for c in st["official"]:
                        if c["id"] == cid and not c.get("fixed"):
                            c["status"] = "deprecated"
                    _save_collections_state(REPO_ROOT, st)
                    return self._json({"ok": True})
            if url.path == "/api/collections/restore":
                cid = data.get("id", "")
                with sim.lock:
                    st = sim.collections_state
                    for c in st["official"]:
                        if c["id"] == cid:
                            c["status"] = "active"
                    if cid in st.get("removed", []):
                        st["removed"].remove(cid)
                        # re-add to official pool as active
                        default = _default_collections_state()
                        for c in default["official"]:
                            if c["id"] == cid:
                                c["status"] = "active"
                                st["official"].append(c)
                                break
                    _save_collections_state(REPO_ROOT, st)
                    return self._json({"ok": True})
            if url.path == "/api/collections/remove":
                cid = data.get("id", "")
                with sim.lock:
                    st = sim.collections_state
                    # If removing an AI-approved collection, send it back to the pool
                    # (don't add to removed list — it lives in ai_candidates)
                    is_ai = False
                    for c in st["official"]:
                        if c["id"] == cid and c.get("ai_approved"):
                            is_ai = True
                            for cand in st.get("ai_candidates", []):
                                if cand["id"] == cid:
                                    cand["status"] = "pending"
                            # Clear any pin
                            kind = f"ai_{cid}"
                            if kind in sim.rail_config.get("pinned", {}):
                                del sim.rail_config["pinned"][kind]
                            break
                    st["official"] = [c for c in st["official"]
                                     if c["id"] != cid or c.get("fixed")]
                    if not is_ai and cid not in st.get("removed", []):
                        st.setdefault("removed", []).append(cid)
                    _save_collections_state(REPO_ROOT, st)
                    return self._json({"ok": True})
            if url.path == "/api/collections/approve":
                cid = data.get("id", "")
                with sim.lock:
                    st = sim.collections_state
                    for c in st.get("ai_candidates", []):
                        if c["id"] == cid and c["status"] == "pending":
                            c["status"] = "approved"
                            st["official"].append({
                                "id": cid, "title": c["title"],
                                "description": c["description"],
                                "fixed": False, "status": "active",
                                "ai_approved": True,
                                "approved_day": sim.day,
                            })
                    _save_collections_state(REPO_ROOT, st)
                    return self._json({"ok": True})
            if url.path == "/api/collections/reject":
                cid = data.get("id", "")
                with sim.lock:
                    st = sim.collections_state
                    for c in st.get("ai_candidates", []):
                        if c["id"] == cid and c["status"] == "pending":
                            c["status"] = "rejected"
                    _save_collections_state(REPO_ROOT, st)
                    return self._json({"ok": True})
            if url.path == "/api/collections/reset_ai":
                # Reset an AI candidate to pending (removes from official).
                # For demo purposes: re-run the approve flow.
                cid = data.get("id", "")
                with sim.lock:
                    st = sim.collections_state
                    st["official"] = [c for c in st.get("official", [])
                                      if c.get("id") != cid]
                    for c in st.get("ai_candidates", []):
                        if c["id"] == cid:
                            c["status"] = "pending"
                    # Clear any pin on the reset collection
                    kind = f"ai_{cid}"
                    if kind in sim.rail_config.get("pinned", {}):
                        del sim.rail_config["pinned"][kind]
                    _save_collections_state(REPO_ROOT, st)
                    return self._json({"ok": True})
            if url.path == "/api/app_campaign":
                with sim.lock:
                    return self._json(sim.start_app_campaign(
                        data.get("provider", ""),
                        _coverage(data), _cluster(data),
                        _days(data), _start_day(data, sim),
                        data.get("placements")))
            if url.path == "/api/featured_campaign":
                with sim.lock:
                    return self._json(sim.start_featured_campaign(
                        data.get("titles", []),
                        _coverage(data), _cluster(data),
                        _days(data), _start_day(data, sim)))
            if url.path == "/api/search_campaign":
                with sim.lock:
                    return self._json(sim.start_search_campaign(
                        data.get("titles", []),
                        _coverage(data), _cluster(data),
                        _days(data), _start_day(data, sim)))
            if url.path == "/api/takeover_campaign":
                with sim.lock:
                    return self._json(sim.start_takeover_campaign(
                        data.get("title", ""),
                        data.get("headline", ""),
                        _coverage(data), _cluster(data),
                        _days(data), _start_day(data, sim),
                        data.get("sponsor", "")))
            if url.path == "/api/takeover_dismiss":
                with sim.lock:
                    cid = data.get("campaign_id")
                    aid = data.get("agent_id", "")
                    for camp in sim.campaigns:
                        if camp.get("id") == cid:
                            camp.setdefault("takeover_dismissals", set()).add(
                                (aid, sim.day))
                            break
                    return self._json({"ok": True})
            if url.path == "/api/takeover_roi":
                with sim.lock:
                    return self._json(sim.takeover_roi())
            if url.path == "/api/coldstart":
                picks = data.get("picks", [])
                picks = [p for p in picks if isinstance(p, str)][:3]
                with sim.lock:
                    taste = sim.coldstart_taste(picks)
                    top = sorted(
                        ((g, round(float(w), 4))
                         for g, w in zip(sim.genres, taste)),
                        key=lambda gv: -gv[1])[:5]
                    return self._json({
                        "picks": picks,
                        "taste": {g: round(float(w), 4)
                                  for g, w in zip(sim.genres, taste)},
                        "top_genres": [g for g, _ in top],
                        "home_screen": sim.coldstart_home(taste),
                    })
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
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    ap.add_argument("--tick", type=float, default=1.5,
                    help="seconds per simulated day")
    ap.add_argument("--agents", type=int, default=240)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--static", default=None)
    args = ap.parse_args()

    STATIC_DIR = args.static or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                             "static")
    sim = LiveSim(n_agents=args.agents, seed=args.seed, tick_seconds=args.tick)
    Handler.sim = sim
    t = threading.Thread(target=sim.loop, daemon=True)
    t.start()
    srv = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    print(f"Synth Farm LIVE  →  http://localhost:{args.port}")
    print(f"  {args.agents} agents, 1 tick = 1 simulated day every {args.tick}s. Press Play.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping…")


if __name__ == "__main__":
    main()
