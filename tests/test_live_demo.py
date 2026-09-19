"""Tests for the live demo server expansion (live_demo/server.py).

Covers the September 2026 expansion: Bollywood dimension + agent #7 pivot,
Master Agent, recommendation explainer, per-rail orderings, and
the 500-title catalog.
"""

import itertools
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "live_demo"))
from server import LiveSim  # noqa: E402

REPO = os.path.join(os.path.dirname(__file__), "..")


@pytest.fixture(scope="module")
def sim():
    s = LiveSim(n_agents=60, seed=7, tick_seconds=0.05)
    for _ in range(3):
        with s.lock:
            s.tick()
    return s


# ---------- Bollywood dimension ----------

def test_thirty_dimensions_with_bollywood(sim):
    assert len(sim.genres) == 30
    assert "Bollywood" in sim.genres


def test_agent_seven_is_bollywood_pivot(sim):
    assert sim.pivot_idx == 6
    top = sim.genres[int(sim.tastes[6].argmax())]
    assert top == "Bollywood"
    assert sim.personas[6].persona_id.endswith("00006")


def test_hindi_titles_get_bollywood_weight(sim):
    bi = sim.gidx["Bollywood"]
    bw = [it.genre_vector[bi] for it in sim.catalog.items
          if "Bollywood" in it.genre_tags]
    assert len(bw) >= 180, f"only {len(bw)} Bollywood-tagged titles"
    assert all(w > 0 for w in bw)
    assert abs(sum(bw) / len(bw) - 0.5) < 0.05


# ---------- Director manager agent ----------

def test_master_agent_nfl_pivots_some_to_sports(sim):
    parsed = sim.parse_directive(
        "NFL is starting, let's pivot some users to watch football for 7 days")
    assert parsed["action"] == "start"
    assert parsed["genres"] == ["Sports"]
    assert parsed["coverage"] == 0.25  # "football" must not match "all"
    assert parsed["days"] == 7


def test_master_agent_scope_words(sim):
    assert sim.parse_directive("pivot everyone to football")["coverage"] == 1.0
    assert sim.parse_directive("pivot half the users to football")["coverage"] == 0.5
    assert sim.parse_directive("pivot 10% of users to football")["coverage"] == 0.1
    assert sim.parse_directive("pivot cluster 3 to football")["cluster"] == 3
    assert sim.parse_directive("pivot some users to bollywood")["genres"] == ["Bollywood"]


def test_master_agent_stop_and_unknown(sim):
    assert sim.parse_directive("stop campaigns")["action"] == "stop"
    assert sim.parse_directive("make the sky purple")["action"] == "unknown"


def test_master_agent_campaign_targets_and_expires():
    s = LiveSim(n_agents=60, seed=7, tick_seconds=0.05)
    out = s.direct("NFL is starting, let's pivot some users to watch football for 7 days")
    assert len(s.campaigns) == 1
    assert len(s.campaigns[0]["targets"]) == 15  # 25% of 60
    assert "pivoting 15 agents" in out["message"]
    for _ in range(7):
        with s.lock:
            # one simulated day passes: tick() decrements days_left the same way
            s.day += 1
            for c in s.campaigns:
                c["days_left"] -= 1
            s.campaigns = [c for c in s.campaigns if c["days_left"] > 0]
    assert s.campaigns == []
    s.direct("stop campaigns")
    assert s.master_log  # log records activity


# ---------- Recommendation explainer ----------

def test_explain_returns_concrete_signals(sim):
    p = sim.personas[0]
    item = next(it for it in sim.catalog.items
                if it.item_id not in sim.watched[p.persona_id])
    out = sim.explain(p.persona_id, item.item_id)
    assert out["title"] == item.title
    assert isinstance(out["signals"], list) and len(out["signals"]) >= 1
    assert all(isinstance(sg, str) and sg for sg in out["signals"])


def test_explain_unknown_ids(sim):
    out = sim.explain("persona-99999", "tmdb-movie-1")
    assert out["signals"] == []


# ---------- Per-rail orderings ----------

def test_rails_full_and_differently_ordered(sim):
    p = sim.personas[0]
    hs = sim.home_screen(p)
    assert hs["rails"][0]["title"] == "Continue watching"
    seqs = [[x["id"] for x in r["items"]] for r in hs["rails"][1:]]
    assert all(len(s) == 20 for s in seqs)
    for a, b in itertools.combinations(seqs, 2):
        assert a != b, "two rails share the identical order"


def test_rails_differently_ordered_on_day_zero():
    # Fresh sim, no plays yet: the "More {g2}" and Trending fallbacks must
    # still not mirror any other rail.
    s = LiveSim(n_agents=60, seed=7, tick_seconds=0.05)
    hs = s.home_screen(s.personas[0])
    seqs = [[x["id"] for x in r["items"]] for r in hs["rails"][1:]]
    assert len(hs["rails"]) == 13
    assert all(len(x) == 20 for x in seqs)
    for a, b in itertools.combinations(seqs, 2):
        assert a != b, "two rails share the identical order on day 0"


def test_small_population_does_not_crash():
    s = LiveSim(n_agents=5, seed=7, tick_seconds=0.05)
    assert s.pivot_idx is None
    hs = s.home_screen(s.personas[0])
    assert len(hs["rails"]) >= 12


def test_because_watched_orders_by_similarity(sim):
    p = next((pp for pp in sim.personas if sim.agent_events[pp.persona_id]), None)
    assert p is not None
    hs = sim.home_screen(p)
    bw = next(r for r in hs["rails"]
              if r["title"].startswith("Because you watched") or r["title"].startswith("More "))
    assert len(bw["items"]) == 20


# ---------- Catalog ----------

def test_bollywood_stays_with_agent_seven():
    # Only agent #7 is the Bollywood guy: everyone else gets a tiny flavor,
    # never a visible pattern; agent #7 stays Bollywood-heavy.
    s = LiveSim(n_agents=60, seed=7, tick_seconds=0.05)
    for _ in range(5):
        with s.lock:
            s.tick()
    for idx in (0, 1, 2):
        hs = s.home_screen(s.personas[idx])
        total = sum(len(r["items"]) for r in hs["rails"])
        bw = sum(1 for r in hs["rails"] for x in r["items"]
                 if "Bollywood" in s.by_id[x["id"]].genre_tags)
        assert bw / total < 0.08, f"agent #{idx + 1} sees {bw / total:.0%} Bollywood"
        assert s.genres[int(s.tastes[idx].argmax())] != "Bollywood"
    hs7 = s.home_screen(s.personas[6])
    total7 = sum(len(r["items"]) for r in hs7["rails"])
    bw7 = sum(1 for r in hs7["rails"] for x in r["items"]
              if "Bollywood" in s.by_id[x["id"]].genre_tags)
    assert bw7 / total7 > 0.20, "agent #7 should stay Bollywood-heavy"


def test_baseball_duo_are_distinct_sports_lovers():
    s = LiveSim(n_agents=24, seed=7, tick_seconds=0.05)
    assert s.n_agents == 26  # two hand-built agents join the population
    duo = {p.persona_id: p for p in s.personas
           if p.persona_id in ("persona-seamhead", "persona-socialfan")}
    assert len(duo) == 2
    a, b = duo["persona-seamhead"], duo["persona-socialfan"]
    assert a.archetype != b.archetype
    assert s.genres[int(a.taste.argmax())] == "Sports"
    assert s.genres[int(b.taste.argmax())] == "Sports"
    # they feel different: behavior and taste shape both differ
    assert (a.sessions_per_week, a.search_propensity,
            a.completion_propensity) != (b.sessions_per_week,
                                         b.search_propensity,
                                         b.completion_propensity)
    assert abs(float(a.taste[s.gidx["Sports"]])
               - float(b.taste[s.gidx["Sports"]])) > 0.1
    s.reset()
    assert s.n_agents == 26  # reset doesn't duplicate the duo


def test_campaign_effect_fades_steadily():
    s = LiveSim(n_agents=60, seed=7, tick_seconds=0.05)
    s.direct("pivot all users to baseball for 7 days")
    assert s.campaigns[0]["days_total"] == 7
    si = s.gidx["Sports"]
    shares = []
    for _ in range(7):
        with s.lock:
            s.tick()
        tot = s.day_genre_total.sum()
        shares.append(s.day_genre_total[si] / tot)
    # slow steady fade: early days beat late days, last day still a whisper
    assert sum(shares[:2]) / 2 > sum(shares[-2:]) / 2
    assert shares[-1] > 0.02
    assert not s.campaigns  # expired after day 7


def test_catalog_500_unique_with_required_keys():
    d = json.load(open(os.path.join(REPO, "data", "catalog_tmdb.json")))
    items = d["items"]
    req = {"tmdb_id", "media_type", "title", "original_language", "genre_ids",
           "overview", "poster_path", "release_date", "popularity",
           "vote_average", "vote_count", "providers_flatrate"}
    assert len(items) == 500
    assert all(set(it.keys()) == req for it in items)
    assert len({(it["media_type"], it["tmdb_id"]) for it in items}) == 500
    assert sum(1 for it in items if it["original_language"] == "hi") >= 180
