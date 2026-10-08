"""Elections coverage campaign (midterms tentpole).

Covers: directive parsing, campaign creation, the LIVE Peacock tile at
featured position 0, the Election coverage rail pinned at position 4,
and expiry cleanup.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "live_demo"))
from server import LiveSim  # noqa: E402


def make_sim(n_agents=24, seed=7):
    return LiveSim(seed=seed, n_agents=n_agents)


def start_elections(sim):
    sim.direct("pivot all users to elections related content for 14 days")
    camp = sim.campaigns[-1]
    assert camp["campaign_type"] == "elections"
    return camp


def test_directive_parses_elections():
    sim = make_sim()
    parsed = sim.parse_directive(
        "pivot all users to elections related content for 14 days")
    assert parsed["action"] == "start"
    assert parsed["campaign_type"] == "elections"
    assert parsed["coverage"] == 1.0
    assert parsed["days"] == 14


def test_elections_campaign_targets_all_agents():
    sim = make_sim()
    camp = start_elections(sim)
    assert len(camp["targets"]) == sim.n_agents
    assert camp["days_total"] == 14
    assert camp["titles"], "should pin election titles"


def test_live_tile_first_in_featured():
    sim = make_sim()
    camp = start_elections(sim)
    pi = next(iter(camp["targets"]))
    home = sim.home_screen(sim.personas[pi])
    feat = next(r for r in home["rails"] if r.get("kind") == "featured")
    first = feat["items"][0]
    assert first.get("live") is True
    assert first["title"] == "Election Coverage Live"
    assert "Peacock" in first["providers"]


def test_elections_rail_at_position_4():
    sim = make_sim()
    camp = start_elections(sim)
    pi = next(iter(camp["targets"]))
    home = sim.home_screen(sim.personas[pi])
    kinds = [r.get("kind") for r in home["rails"]]
    assert kinds[0] == "featured"
    assert kinds[1] == "apps"
    assert kinds[2] == "continue"
    assert kinds[3] == "elections", f"rail order: {kinds[:6]}"
    rail = home["rails"][3]
    assert rail["title"] == "Election coverage"
    assert rail["items"], "rail should have election titles"


def test_no_elections_rail_without_campaign():
    sim = make_sim()
    home = sim.home_screen(sim.personas[0])
    kinds = [r.get("kind") for r in home["rails"]]
    assert "elections" not in kinds
    feat = next(r for r in home["rails"] if r.get("kind") == "featured")
    assert not any(t.get("live") for t in feat["items"])


def test_elections_campaign_expiry_cleans_up():
    sim = make_sim()
    camp = start_elections(sim)
    pi = next(iter(camp["targets"]))
    # fast-forward past the 14 days
    for _ in range(15):
        sim.tick()
    assert not [c for c in sim.campaigns
                if c.get("campaign_type") == "elections"]
    home = sim.home_screen(sim.personas[pi])
    kinds = [r.get("kind") for r in home["rails"]]
    assert "elections" not in kinds
