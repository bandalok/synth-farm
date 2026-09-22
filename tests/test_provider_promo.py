"""Tests for the FoxOne subscription-promotion campaign feature
(live_demo/server.py — provider_promo campaigns).

Covers: directive parsing (fox variants + genre/stop regressions), acquisition
targeting (non-subscribers only, zero-eligible safety), the "Trending on
FoxOne" rail (content + placement), click-to-subscribe conversion, the
never-modify-taste guarantee (with a genre campaign as a shifting control),
closed-loop analytics, campaign JSON serialization + history retention, catalog
invariants (700 titles, 12 FOX One titles, posters), and scheduled campaigns.
"""

import os
import sys

import json
import subprocess

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "live_demo"))
from server import LiveSim  # noqa: E402


PROMO_DIRECTIVE = (
    "run FoxOne promotion to those who do not have active subscription of it"
)

EXPECTED_FOX_ONE_TITLES = {
    "The Simpsons", "American Dad!", "Family Guy", "Bob's Burgers",
    "The Masked Singer", "Hell's Kitchen", "The Floor", "Animal Control",
    "Murder in a Small Town", "Krapopolis", "Extracted", "Universal Basic Guys",
}


def make_sim(n_agents=24, seed=7):
    return LiveSim(n_agents=n_agents, seed=seed, tick_seconds=0.05)


def fox_title_ids(sim):
    return {it.item_id for it in sim.catalog.items if "FOX One" in it.providers}


def start_promo(sim, directive=PROMO_DIRECTIVE):
    """Run a promo directive; return the created provider_promo campaign."""
    sim.direct(directive)
    camp = sim.campaigns[-1]
    assert camp["campaign_type"] == "provider_promo"
    return camp


def promo_rail(home):
    return next((r for r in home["rails"]
                 if r.get("key") == "provider_promo"), None)


def promo_analytics_entry(sim):
    entries = [e for e in sim.campaign_analytics()
               if e.get("campaign_type") == "provider_promo"]
    assert entries, "expected a provider_promo analytics entry"
    return entries[0]


# ---------- 1. Directive parsing ----------

@pytest.mark.parametrize("variant", [
    "run FoxOne promotion to those who don't have active subscription of it",
    "run a fox one promotion to non-subscribers for 7 days",
    "run a fox-one promotion to non-subscribers for 7 days",
    "run a fox 1 promotion to non-subscribers for 7 days",
])
def test_parse_fox_variants(variant):
    sim = make_sim()
    parsed = sim.parse_directive(variant)
    assert parsed["action"] == "start"
    assert parsed["campaign_type"] == "provider_promo"
    assert parsed["provider"] == "FOX One"


def test_parse_genre_directive_regression():
    sim = make_sim()
    parsed = sim.parse_directive("pivot some users to football for 7 days")
    assert parsed["action"] == "start"
    assert parsed["genres"] == ["Sports"]
    assert parsed.get("campaign_type") is None  # genre path sets no promo type


def test_parse_stop_campaign():
    sim = make_sim()
    assert sim.parse_directive("stop campaign")["action"] == "stop"


def test_direct_stop_campaign_clears_promo():
    sim = make_sim()
    start_promo(sim)
    assert sim.campaigns
    sim.direct("stop campaign")
    assert sim.campaigns == []
    assert any(h.get("campaign_type") == "provider_promo"
               for h in sim.campaign_history)


# ---------- 2. Targeting ----------

def test_targeting_only_non_subscribers():
    sim = make_sim()
    eligible = {i for i, p in enumerate(sim.personas)
                if "FOX One" not in p.subscribed_apps}
    assert eligible, "test needs at least one eligible agent"
    camp = start_promo(sim)
    targets = set(camp["targets"])
    assert targets <= eligible
    assert len(targets) <= len(eligible)
    for i, p in enumerate(sim.personas):
        if "FOX One" in p.subscribed_apps:
            assert i not in targets


def test_zero_eligible_targets_no_crash():
    sim = make_sim()
    for p in sim.personas:
        if "FOX One" not in p.subscribed_apps:
            p.subscribed_apps = tuple(p.subscribed_apps) + ("FOX One",)
    res = sim.direct(PROMO_DIRECTIVE)  # must not raise
    camp = sim.campaigns[-1]
    assert camp["targets"] == set()
    assert res["campaigns"][-1]["n_targets"] == 0
    home = sim.home_screen(sim.personas[0])
    assert promo_rail(home) is None
    entry = promo_analytics_entry(sim)
    assert entry["n_targets"] == 0
    assert entry["conversion_rate"] is None


# ---------- 3. Rail content ----------

def test_rail_content_for_targeted_agent():
    sim = make_sim()
    camp = start_promo(sim)
    assert camp["targets"], "test needs at least one targeted agent"
    pi = next(iter(camp["targets"]))
    home = sim.home_screen(sim.personas[pi])
    rail = promo_rail(home)
    assert rail is not None
    assert rail["title"] == "Trending on FoxOne"
    items = rail["items"]
    assert items, "promo rail must carry pinned titles"
    ids = [d["id"] for d in items]
    assert len(ids) == len(set(ids)), "no within-rail duplicates"
    assert set(ids) <= set(camp["titles"])
    for d in items:
        assert d["providers"], "every tile needs a non-empty provider list"
        assert "FOX One" in d["providers"]
        assert d.get("why_hover"), "every tile needs a why_hover explanation"
        assert d.get("promoted") is True


def test_no_rail_for_non_targeted_agent():
    sim = make_sim()
    camp = start_promo(sim)
    non_targets = [i for i in range(sim.n_agents) if i not in camp["targets"]]
    assert non_targets
    home = sim.home_screen(sim.personas[non_targets[0]])
    assert promo_rail(home) is None


# ---------- 4. Placement ----------

@pytest.mark.parametrize("seed", [7, 42, 123])
def test_promo_rail_placement_across_seeds(seed):
    sim = make_sim(seed=seed)
    camp = start_promo(sim)
    targets = sorted(camp["targets"])[:5]
    assert targets, "test needs targeted agents"
    for pi in targets:
        home = sim.home_screen(sim.personas[pi])
        titles = [r["title"] for r in home["rails"]]
        assert home["rails"][0]["title"] == "Continue watching"
        idx = next(i for i, r in enumerate(home["rails"])
                   if r.get("key") == "provider_promo")
        assert idx >= 2, f"promo rail displaced a top rail (idx {idx})"


def test_no_promo_rail_without_campaigns():
    sim = make_sim()
    home = sim.home_screen(sim.personas[0])
    assert promo_rail(home) is None
    assert "Trending on FoxOne" not in [r["title"] for r in home["rails"]]


# ---------- 5. Conversion ----------

def test_click_converts_and_excludes_from_next_promo():
    sim = make_sim()
    camp = start_promo(sim)
    # Highest clickiness targeted agent converts fastest.
    pi = max(camp["targets"], key=lambda i: sim.personas[i].clickiness)
    p = sim.personas[pi]
    assert "FOX One" not in p.subscribed_apps
    rail = promo_rail(sim.home_screen(p))
    tid = rail["items"][0]["id"]
    for _ in range(200):
        sim.click(p.persona_id, tid)
        if "FOX One" in p.subscribed_apps:
            break
    assert "FOX One" in p.subscribed_apps, "bounded click loop must convert"
    convs = [e for e in sim.agent_events[p.persona_id]
             if e.get("type") == "conversion"]
    assert convs, "expected a conversion event in agent_events"
    assert convs[0]["campaign_id"] == camp["id"]
    assert convs[0]["provider"] == "FOX One"
    # A subsequent acquisition campaign must skip the converted agent.
    sim.direct("run a FoxOne promotion to all users who do not have it")
    new_camp = sim.campaigns[-1]
    eligible = {i for i, q in enumerate(sim.personas)
                if "FOX One" not in q.subscribed_apps}
    assert pi not in new_camp["targets"]
    assert set(new_camp["targets"]) == eligible


def test_promo_campaign_never_modifies_taste():
    """provider_promo has weight 0.0 / vec None and its steering blend is
    gated off: identical tastes to a no-campaign run, unlike a genre
    campaign (the shifting control)."""

    def manual_campaign(sim, promo):
        n = sim.n_agents
        if promo:
            return {"id": 1, "text": "manual promo",
                    "campaign_type": "provider_promo", "provider": "FOX One",
                    "genres": [], "targets": set(range(n)),
                    "titles": fox_title_ids(sim), "impressions": set(),
                    "vec": None, "weight": 0.0,
                    "days_left": 7, "days_total": 7,
                    "created_day": 0, "start_day": 0}
        vec = np.zeros(len(sim.genres))
        vec[sim.gidx["Sports"]] = 1.0
        return {"id": 1, "text": "manual genre",
                "campaign_type": "genre_pivot", "genres": ["Sports"],
                "targets": set(range(n)), "vec": vec, "weight": 0.45,
                "days_left": 7, "days_total": 7,
                "created_day": 0, "start_day": 0}

    base, promo_sim, genre_sim = make_sim(), make_sim(), make_sim()
    promo_sim.campaigns.append(manual_campaign(promo_sim, promo=True))
    genre_sim.campaigns.append(manual_campaign(genre_sim, promo=False))
    for s in (base, promo_sim, genre_sim):
        with s.lock:
            for _ in range(3):
                s.tick()
    assert np.array_equal(base.tastes, promo_sim.tastes), \
        "promo campaign must leave taste evolution untouched"
    assert not np.array_equal(base.tastes, genre_sim.tastes), \
        "genre campaign control must shift tastes (sanity check)"


def test_promo_campaign_structural_neutrality():
    sim = make_sim()
    camp = start_promo(sim)
    assert camp["campaign_type"] == "provider_promo"
    assert camp["provider"] == "FOX One"
    assert camp["genres"] == []
    assert camp["vec"] is None
    assert camp["weight"] == 0.0


# ---------- 7. Analytics ----------

def test_promo_analytics_closed_loop():
    sim = make_sim()
    camp = start_promo(sim)
    with sim.lock:
        sim.tick()  # day 1: events now fall inside the analytics window
    for i in camp["targets"]:
        sim.home_screen(sim.personas[i])  # register impressions
    pi = max(camp["targets"], key=lambda i: sim.personas[i].clickiness)
    p = sim.personas[pi]
    rail = promo_rail(sim.home_screen(p))
    tid = rail["items"][0]["id"]
    for _ in range(200):
        sim.click(p.persona_id, tid)
        if "FOX One" in p.subscribed_apps:
            break
    assert "FOX One" in p.subscribed_apps
    entry = promo_analytics_entry(sim)
    assert entry["impressions"] > 0
    assert entry["clicks"] >= 0
    assert entry["conversions"] >= 1
    rate = entry["conversion_rate"]
    assert rate is None or 0 <= rate <= 1
    assert entry["provider"] == "FOX One"
    assert entry["genres"] == []
    assert entry["lift"] is None
    assert entry["taste_shift_pp"] is None
    assert entry["plays_live"] == 0 and entry["plays_baseline"] == 0


def test_genre_analytics_keys_unchanged():
    sim = make_sim()
    sim.direct("pivot some users to football for 7 days")
    with sim.lock:
        sim.tick()
    genre_entries = [e for e in sim.campaign_analytics()
                     if e.get("campaign_type") != "provider_promo"]
    assert genre_entries
    entry = genre_entries[0]
    assert "lift" in entry and "taste_shift_pp" in entry
    assert "plays_live" in entry and "plays_baseline" in entry


# ---------- 8. Serialization ----------

def test_campaigns_json_promo_fields():
    sim = make_sim()
    start_promo(sim)
    entry = sim._campaigns_json()[-1]
    assert entry["campaign_type"] == "provider_promo"
    assert entry["provider"] == "FOX One"
    expected = sorted(fox_title_ids(sim))
    assert entry["titles"] == expected
    assert entry["titles"] == sorted(entry["titles"])
    assert entry["status"] == "live"


def test_expired_promo_retains_fields_in_history():
    sim = make_sim()
    sim.direct("run FoxOne promotion for 1 day")
    with sim.lock:
        sim.tick()  # days_left 1 -> 0: campaign expires into history
    assert not sim.campaigns
    hist = sim.campaign_history[-1]
    assert hist["campaign_type"] == "provider_promo"
    assert hist["provider"] == "FOX One"
    assert hist["titles"] == sorted(fox_title_ids(sim))
    assert hist["live"] is False
    ended = [e for e in sim.campaign_analytics()
             if e.get("campaign_type") == "provider_promo"][-1]
    assert ended["status"] == "ended"


# ---------- 9. Catalog invariants ----------

def test_catalog_size_and_posters():
    sim = make_sim()
    assert len(sim.catalog.items) == 700
    assert all(it.poster_path for it in sim.catalog.items), \
        "every catalog entry must carry a poster"


def test_fox_one_titles_have_posters():
    sim = make_sim()
    fox_items = [it for it in sim.catalog.items if "FOX One" in it.providers]
    assert len(fox_items) == 12, f"expected 12 FOX One titles, got {len(fox_items)}"
    assert {it.title for it in fox_items} == EXPECTED_FOX_ONE_TITLES
    assert all(it.poster_path for it in fox_items)


# ---------- 10. Scheduling ----------

def test_scheduled_promo_starts_and_decrements():
    sim = make_sim()
    camp = start_promo(sim, "run FoxOne promotion starting day 3 for 5 days")
    entry = sim._campaigns_json()[-1]
    assert entry["status"] == "scheduled"
    pi = next(iter(camp["targets"])) if camp["targets"] else 0
    with sim.lock:
        sim.tick()
        sim.tick()
    assert sim.day == 2
    assert camp["days_left"] == 5, "days_left must not decay before start_day"
    assert not sim._camp_active(camp)
    assert promo_rail(sim.home_screen(sim.personas[pi])) is None
    with sim.lock:
        sim.tick()
    assert sim.day == 3
    assert sim._camp_active(camp)
    assert camp["days_left"] == 4, "days_left must decrement while live"
    rail = promo_rail(sim.home_screen(sim.personas[pi]))
    assert rail is not None
    assert sim._campaigns_json()[-1]["status"] == "live"


# ---------- 15. FoxOne glow fix: /api/agents target labels ----------

def _api_agents_labels(sim, pi):
    """Replicates the /api/agents handler's targeted/scheduled labeling
    comprehension exactly, using the same LiveSim._campaign_target_labels
    helper the handler now calls. (No HTTP test harness exists in this
    suite, so the labels are computed the same way the handler computes
    them — the helper is the shared point of truth.)"""
    return (
        sorted({label for c in sim.campaigns
                for label in sim._campaign_target_labels(c)
                if pi in c["targets"] and sim._camp_active(c)}),
        sorted({label for c in sim.campaigns
                for label in sim._campaign_target_labels(c)
                if pi in c["targets"] and not sim._camp_active(c)}),
    )


def test_target_labels_helper_promo_returns_provider():
    sim = make_sim()
    camp = start_promo(sim)
    assert sim._campaign_target_labels(camp) == ["FOX One"]


def test_target_labels_helper_genre_returns_genres():
    sim = make_sim()
    sim.direct("pivot some users to football for 7 days")
    camp = sim.campaigns[-1]
    assert camp["genres"] == ["Sports"]  # not a provider_promo campaign
    assert sim._campaign_target_labels(camp) == ["Sports"]


def test_target_labels_helper_provider_fallback():
    sim = make_sim()
    assert sim._campaign_target_labels(
        {"campaign_type": "provider_promo", "provider": None, "genres": []}
    ) == ["promotion"]


def test_promo_target_gets_fox_one_label():
    sim = make_sim()
    camp = start_promo(sim)
    assert sim._camp_active(camp)
    pi = next(iter(camp["targets"]))
    targeted, _scheduled = _api_agents_labels(sim, pi)
    assert targeted == ["FOX One"]
    non_target = next(i for i in range(sim.n_agents)
                      if i not in camp["targets"])
    assert _api_agents_labels(sim, non_target) == ([], [])


def test_genre_campaign_labels_unchanged():
    """Genre-campaign output must be byte-identical to the pre-fix handler,
    which iterated c['genres'] directly."""
    sim = make_sim()
    sim.direct("pivot some users to football for 7 days")
    camp = sim.campaigns[-1]
    pi = next(iter(camp["targets"]))
    targeted, scheduled = _api_agents_labels(sim, pi)
    # old handler shape, computed without the helper, as the regression baseline
    old_targeted = sorted({g for g in camp["genres"]
                           if pi in camp["targets"]
                           and sim._camp_active(camp)})
    assert targeted == old_targeted == ["Sports"]
    assert scheduled == []


def test_frontend_card_glow_conditionals_with_promo_payload():
    """The agent card (live_demo/static/app.js, the card div and the badge)
    keys glow + badge ENTIRELY off a.targeted, a generic consumer: any
    non-empty list triggers the glow class and the badge join. Since the
    fixed server now sends targeted=['FOX One'], the frontend needs no
    change. Evaluate the card's exact conditionals via node against a
    promo-shaped payload, and show the pre-fix payload ([]) stayed dark."""
    js = r"""
    // mirrored verbatim from app.js: campLabel (line 179) + the card div
    // class conditional and badge conditional (renderAgentCards).
    const campLabel = (g) => g === "Sports" ? "Baseball" : g;
    const esc = (s) => String(s);
    const run = (a) => ({
      cls: (a.targeted && a.targeted.length) ? " targeted" : "",
      badge: (a.targeted && a.targeted.length)
        ? ` · <span class="tgt">🎯 ${esc(a.targeted.map(campLabel).join(" + "))} energy</span>` : "",
    });
    process.stdout.write(JSON.stringify({
      fixed: run({targeted: ["FOX One"]}),  // what fixed /api/agents sends
      preFix: run({targeted: []}),          // what it used to send (no glow)
    }));
    """
    out = subprocess.run(["node", "-e", js], capture_output=True,
                         text=True, check=True).stdout
    res = json.loads(out)
    assert res["fixed"]["cls"] == " targeted", "glow class must fire"
    assert "FOX One energy" in res["fixed"]["badge"], \
        "badge must read '🎯 FOX One energy'"
    assert res["preFix"]["cls"] == "" and res["preFix"]["badge"] == "", \
        "pre-fix payload (empty list) never glowed — the documented bug"
