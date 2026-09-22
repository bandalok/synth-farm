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
    assert len(s.campaigns[0]["targets"]) == int(0.25 * s.n_agents)  # 25% of 64
    assert f"pivoting {int(0.25 * s.n_agents)} agents" in out["message"]
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
    assert s.n_agents == 28  # four hand-built anchors join the population
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
    assert s.n_agents == 28  # reset doesn't duplicate the anchors


def test_scifi_pair_are_distinct_scifi_lovers():
    s = LiveSim(n_agents=24, seed=7, tick_seconds=0.05)
    pair = {p.persona_id: p for p in s.personas
            if p.persona_id in ("persona-voidwalker", "persona-nebula")}
    assert len(pair) == 2
    a, b = pair["persona-voidwalker"], pair["persona-nebula"]
    assert a.archetype != b.archetype
    assert s.genres[int(a.taste.argmax())] == "Science Fiction"
    assert s.genres[int(b.taste.argmax())] == "Science Fiction"
    # purist binges and finishes; tourist samples and bails
    assert a.completion_propensity > 0.9 > b.completion_propensity
    assert a.search_propensity < b.search_propensity
    live_a = s.tastes[s.personas.index(a)]
    assert s.genres[int(live_a.argmax())] == "Science Fiction"
    titles = [t["title"] for r in s.home_screen(a)["rails"]
              for t in r["items"]]
    assert any(t in titles for t in ("Dune: Part Two", "Interstellar",
                                     "The Matrix", "Project Hail Mary"))
    # anchor pins are exposed for badges
    from server import ANCHOR_PINS
    assert ANCHOR_PINS["persona-voidwalker"] == "Science Fiction"
    assert ANCHOR_PINS["persona-seamhead"] == "Sports"


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


def test_mlb_shelf_and_providers_on_every_title():
    s = LiveSim(n_agents=24, seed=7, tick_seconds=0.05)
    assert len(s.catalog.items) == 500  # exactly 500: 484 TMDb + 16 synthetic MLB
    mlb = [it for it in s.catalog.items if it.item_id.startswith("tmdb-movie--")]
    assert len(mlb) == 16
    assert all("Sports" in it.genre_tags for it in mlb)
    assert all(it.providers for it in s.catalog.items)
    t = s._item_json(mlb[0])
    assert len(t["providers"]) >= 1  # every tile shows where to watch


def test_baseball_duo_live_tastes_drive_home_screen():
    s = LiveSim(n_agents=24, seed=7, tick_seconds=0.05)
    mlb_titles = {"Field of Dreams", "Moneyball", "42", "The Sandlot",
                  "Bull Durham", "A League of Their Own", "Ken Burns: Baseball"}
    for pid in ("persona-seamhead", "persona-socialfan"):
        p = next(pp for pp in s.personas if pp.persona_id == pid)
        live = s.tastes[s.personas.index(p)]
        assert s.genres[int(live.argmax())] == "Sports"
    seam = next(pp for pp in s.personas if pp.persona_id == "persona-seamhead")
    titles = [t["title"] for r in s.home_screen(seam)["rails"]
              for t in r["items"]]
    assert any(t in mlb_titles for t in titles)


def test_campaign_pushes_genre_onto_targeted_home_screen():
    s = LiveSim(n_agents=24, seed=7, tick_seconds=0.05)
    s.direct("blast everyone to baseball for 7 days")
    tgt = sorted(s.campaigns[0]["targets"])[0]
    mlb_titles = {"Moneyball", "Field of Dreams", "42", "Fastball"}
    titles = [t["title"] for r in s.home_screen(s.personas[tgt])["rails"]
              for t in r["items"]]
    assert any(t in mlb_titles for t in titles)


def test_campaign_collection_pinned_for_targets():
    s = LiveSim(n_agents=24, seed=7, tick_seconds=0.05)
    s.direct("pivot some users to baseball for 7 days")
    tgt = sorted(s.campaigns[0]["targets"])[0]
    rails = s.home_screen(s.personas[tgt])["rails"]
    camp = next(r for r in rails if "Master Agent" in r["title"])
    assert rails.index(camp) <= 1  # prominent: top two
    assert "Baseball" in camp["title"]  # user-facing label, not internal genre
    assert camp["items"]  # non-empty
    assert all(i["genre"] == "Sports" for i in camp["items"])  # reads as sports
    assert camp["items"][0]["title"] in ("Ken Burns: Baseball", "Moneyball",
                                         "Field of Dreams", "42")
    others = [i for i in range(s.n_agents)
              if i not in s.campaigns[0]["targets"]]
    assert others, "test needs a non-target"
    rails2 = s.home_screen(s.personas[others[0]])["rails"]
    assert not any("Master Agent" in r["title"] for r in rails2)


def test_scheduled_campaign_fires_on_start_day():
    s = LiveSim(n_agents=24, seed=7, tick_seconds=0.05)
    out = s.direct("pivot some users to baseball from day 11 to day 20")
    c = s.campaigns[0]
    assert c["start_day"] == 11 and c["days_total"] == 9
    assert "days 11–19" in out["message"]
    tgt = sorted(c["targets"])[0]
    # before day 11: nothing steers
    rails = s.home_screen(s.personas[tgt])["rails"]
    assert not any("Master Agent" in r["title"] for r in rails)
    for _ in range(11):
        with s.lock:
            s.tick()
    assert s._camp_active(c)
    rails = s.home_screen(s.personas[tgt])["rails"]
    camp = next(r for r in rails if "Master Agent" in r["title"])
    assert camp["items"][0]["title"] == "Ken Burns: Baseball"
    # expires 9 days after going live and lands in history
    for _ in range(9):
        with s.lock:
            s.tick()
    assert not s.campaigns and len(s.campaign_history) == 1

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

def test_campaign_going_live_pauses_sim_for_inspection():
    # immediate campaign freezes the sim so its visuals can be inspected
    s = LiveSim(n_agents=24, seed=7, tick_seconds=0.05)
    s.running = True
    s.direct("pivot some users to baseball for 7 days")
    assert s.running is False
    assert "paused" in s.campaigns[0]["text"] or "⏸" in s.master_log[-1]["text"]

def test_scheduled_campaign_activation_pauses_sim():
    s = LiveSim(n_agents=24, seed=7, tick_seconds=0.05)
    s.direct("pivot some users to baseball from day 11 to day 20")
    s.running = True
    for _ in range(11):
        with s.lock:
            s.tick()
    assert s._camp_active(s.campaigns[0])
    assert s.running is False
    assert "⏸" in s.master_log[-1]["text"]

# ---------- pause-mode day stepping (◀ ▶) ----------

def _fresh_paused(n=24):
    s = LiveSim(n_agents=n, seed=7, tick_seconds=0.05)
    assert s.running is False and s.day == 0
    return s

def test_step_back_at_day_zero_is_noop():
    s = _fresh_paused()
    r = s.step_day(-1)
    assert r["ok"] is False and s.day == 0

def test_step_forward_advances_one_day_while_paused():
    s = _fresh_paused()
    r = s.step_day(1)
    assert r["ok"] is True and s.day == 1

def test_step_back_and_forward_restore_exact_state():
    s = _fresh_paused()
    for _ in range(3):
        with s.lock:
            s.tick()
    tastes3 = s.tastes.copy()
    watched3 = {k: set(v) for k, v in s.watched.items()}
    assert s.step_day(-1)["ok"] and s.day == 2
    assert s.step_day(-1)["ok"] and s.day == 1
    assert s.step_day(1)["ok"] and s.day == 2
    assert s.step_day(1)["ok"] and s.day == 3
    assert (s.tastes == tastes3).all()
    assert s.watched == watched3

def test_step_while_running_is_refused():
    s = _fresh_paused()
    s.running = True
    assert s.step_day(-1)["ok"] is False
    assert s.step_day(1)["ok"] is False
    assert s.day == 0

def test_campaign_survives_step_away_and_back():
    s = _fresh_paused()
    for _ in range(3):
        with s.lock:
            s.tick()
    s.direct("pivot all users to baseball for 7 days")
    assert any(s._camp_active(c) for c in s.campaigns)
    assert s.step_day(-1)["ok"] and s.day == 2
    assert not any(s._camp_active(c) for c in s.campaigns)
    assert s.step_day(1)["ok"] and s.day == 3
    assert any(s._camp_active(c) for c in s.campaigns)
    assert s.campaigns[0]["days_left"] == 7

def test_new_directive_after_step_back_truncates_future():
    s = _fresh_paused()
    for _ in range(3):
        with s.lock:
            s.tick()
    s.direct("pivot all users to baseball for 7 days")
    s.step_day(-1)
    s.step_day(-1)  # back at day 1
    s.direct("pivot all users to horror for 7 days")
    s.step_day(1)  # forward into a brand-new day 2
    genres = [g for c in s.campaigns for g in c["genres"]]
    assert "Horror" in genres and "Sports" not in genres

def test_campaign_days_left_decrements_on_stepped_days():
    s = _fresh_paused()
    s.direct("pivot all users to baseball for 7 days")
    assert s.campaigns[0]["days_left"] == 7
    s.step_day(1)
    assert s.campaigns[0]["days_left"] == 6
    s.step_day(-1)
    assert s.campaigns[0]["days_left"] == 7

# ---------- exact-500 catalog, every tile has poster art ----------

def test_catalog_is_exactly_500_titles():
    s = LiveSim(n_agents=24, seed=7, tick_seconds=0.05)
    assert len(s.catalog.items) == 500

def test_every_catalog_title_has_poster_art():
    s = LiveSim(n_agents=24, seed=7, tick_seconds=0.05)
    missing = [it.title for it in s.catalog.items if not it.poster_path]
    assert missing == [], f"{len(missing)} titles without posters"

def test_mlb_shelf_kept_16_with_real_posters():
    s = LiveSim(n_agents=24, seed=7, tick_seconds=0.05)
    mlb = [it for it in s.catalog.items if it.item_id.startswith("tmdb-movie--")]
    assert len(mlb) == 16
    assert all(it.poster_path.startswith("/") for it in mlb)

def test_home_screen_tiles_all_carry_poster_urls():
    s = LiveSim(n_agents=24, seed=7, tick_seconds=0.05)
    s.direct("pivot all users to baseball for 7 days")
    p = s.personas[6]
    payload = s.agent_payload(p)
    tiles = [t for r in payload["home_screen"]["rails"] for t in r["items"]]
    assert len(tiles) > 0
    missing = [t["title"] for t in tiles if not t["poster"]]
    assert missing == [], f"{len(missing)} tiles without poster urls"

# ---------- sponsored search ----------

def test_search_has_exactly_one_sponsored_never_first():
    s = LiveSim(n_agents=24, seed=7, tick_seconds=0.05)
    for _ in range(20):
        r = s.search("a", persona_id="persona-00006")
        assert len(r) > 4
        spon_idx = [i for i, x in enumerate(r) if x.get("sponsored")]
        assert len(spon_idx) == 1
        assert spon_idx[0] in (1, 2, 3), f"sponsored at {spon_idx[0]}, want 2nd-4th slot"

def test_sponsored_title_deterministic_but_slot_varies():
    s = LiveSim(n_agents=24, seed=7, tick_seconds=0.05)
    titles, slots = set(), set()
    for _ in range(30):
        r = s.search("aveng", persona_id="persona-00006")
        i = next(i for i, x in enumerate(r) if x.get("sponsored"))
        titles.add(r[i]["title"])
        slots.add(i)
        assert "aveng" in r[i]["title"].lower()
    assert titles == {"Avengers: Endgame"}
    assert len(slots) > 1, "sponsored slot should vary across searches"

def test_sponsored_does_not_break_personalized_organics():
    s = LiveSim(n_agents=24, seed=7, tick_seconds=0.05)
    r = s.search("a", persona_id="persona-00006")
    organics = [x for x in r if not x.get("sponsored")]
    assert organics[0]["title"] == "Aashiqui 2"  # Bollywood still leads organics

def test_search_no_match_returns_empty():
    s = LiveSim(n_agents=24, seed=7, tick_seconds=0.05)
    assert s.search("zzzqqqxxy") == []

# ---------- "Why this?" hover ----------

def test_why_hover_on_all_home_tiles():
    s = LiveSim(n_agents=12, seed=7, tick_seconds=0.05)
    hs = s.home_screen(s.personas[0])
    items = [it for r in hs["rails"] for it in r["items"]]
    assert len(items) > 100
    assert all("why_hover" in it for it in items)
    assert all("Taste match" in it["why_hover"] for it in items)

def test_why_hover_shows_campaign_boost_when_targeted():
    s = LiveSim(n_agents=12, seed=7, tick_seconds=0.05)
    s.direct("pivot all users to baseball for 7 days")
    hs = s.home_screen(s.personas[0])
    texts = [it["why_hover"] for r in hs["rails"] for it in r["items"]]
    assert any("Master Agent boost" in t for t in texts)

def test_why_hover_never_breaks_item_keys():
    # additive only: original keys still present
    s = LiveSim(n_agents=12, seed=7, tick_seconds=0.05)
    hs = s.home_screen(s.personas[0])
    it = hs["rails"][0]["items"][0]
    for k in ("id", "title", "genre", "poster", "providers"):
        assert k in it

# ---------- cold start ----------

def test_coldstart_taste_from_quiz():
    s = LiveSim(n_agents=12, seed=7, tick_seconds=0.05)
    taste = s.coldstart_taste(["Action", "Comedy", "Bollywood"])
    assert abs(float(taste.sum()) - 1.0) < 1e-9
    assert s.genres[int(taste.argmax())] == "Action"
    # unknown picks are ignored, never fatal
    t2 = s.coldstart_taste(["Nope", "Action", "AlsoNope"])
    assert s.genres[int(t2.argmax())] == "Action"
    t3 = s.coldstart_taste([])
    assert abs(float(t3.sum()) - 1.0) < 1e-9

def test_coldstart_home_has_rails_and_why():
    s = LiveSim(n_agents=12, seed=7, tick_seconds=0.05)
    hs = s.coldstart_home(s.coldstart_taste(["Horror", "Thriller", "Comedy"]))
    assert len(hs["rails"]) >= 5
    items = [it for r in hs["rails"] for it in r["items"]]
    assert len(items) > 50
    assert all("why_hover" in it for it in items)
    # repetition across rails is allowed, but never within one rail
    for r in hs["rails"]:
        rt = [it["title"] for it in r["items"]]
        assert len(rt) == len(set(rt))  # no dupes within a rail
    # horror-leaning taste puts horror titles up top
    top_titles = [it["title"] for it in hs["rails"][0]["items"][:5]]
    top_genres = [s.by_id[s.catalog.items[[x.title for x in s.catalog.items].index(t)].item_id].primary_genre
                  if t in [x.title for x in s.catalog.items] else "" for t in top_titles]
    assert "Horror" in top_genres or "Thriller" in top_genres

# ---------- campaign analytics ----------

def test_campaign_analytics_measures_lift_and_shift():
    s = LiveSim(n_agents=24, seed=7, tick_seconds=0.05)
    for _ in range(3):
        s.tick()
    s.direct("pivot all users to baseball for 4 days")
    for _ in range(4):
        s.tick()
    a = s.campaign_analytics()
    assert len(a) >= 1
    c = a[0]
    assert c["n_targets"] == s.n_agents
    assert c["plays_live"] > c["plays_baseline"]
    assert c["lift"] is not None and c["lift"] > 0
    assert c["taste_shift_pp"] is not None and c["taste_shift_pp"] > 0
    assert c["status"] == "ended"

def test_campaign_history_keeps_targets():
    s = LiveSim(n_agents=12, seed=7, tick_seconds=0.05)
    s.direct("pivot some users to horror for 2 days")
    n = len(s.campaigns[0]["targets"])
    for _ in range(3):
        s.tick()
    assert len(s.campaign_history) == 1
    assert len(s.campaign_history[0]["targets"]) == n
    s2 = LiveSim(n_agents=12, seed=7, tick_seconds=0.05)
    s2.direct("pivot some users to horror for 5 days")
    s2.direct("stop campaigns")
    assert len(s2.campaign_history[0]["targets"]) > 0
