"""Tests for the Master Agent "Recommended campaign" card
(live_demo/server.py — LiveSim.recommendation()).

Covers: the payload shape + launchability on a fresh sim, launchability with
only genre campaigns live, non-launchability with correct live info when a
FOX One provider_promo is active, launchability again after the promo
expires, a scheduled (not yet live) promo not blocking the card, the
directive parsing to provider_promo/FOX One via sim.parse_directive, and
sim.direct() with the directive producing a campaign that matches typing
the directive manually.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "live_demo"))
from server import LiveSim  # noqa: E402


def make_sim(n_agents=24, seed=7):
    return LiveSim(n_agents=n_agents, seed=seed, tick_seconds=0.05)


def promo_directive(sim):
    return sim.recommendation()["directive"]


def expire(campaign, sim):
    """Move a campaign out of the live list the way tick() does when its
    days run out: drop it from sim.campaigns (the expiry sweep keeps only
    days_left > 0)."""
    campaign["days_left"] = 0
    sim.campaigns = [c for c in sim.campaigns if c["days_left"] > 0]


# ---------- payload + launchability ----------

def test_launchable_on_fresh_sim():
    sim = make_sim()
    rec = sim.recommendation()
    assert rec["id"] == "foxone_promo"
    assert rec["title"] == "Fox One subscription push"
    assert rec["description"]
    assert rec["directive"]
    assert rec["launchable"] is True
    assert rec["live"] is None


def test_launchable_with_only_genre_campaigns():
    sim = make_sim()
    sim.direct("pivot some users to football for 7 days")
    assert sim.campaigns
    assert all(c.get("campaign_type") != "provider_promo"
               for c in sim.campaigns)
    rec = sim.recommendation()
    assert rec["launchable"] is True
    assert rec["live"] is None


def test_not_launchable_with_live_promo():
    sim = make_sim()
    sim.direct(promo_directive(sim))
    camp = sim.campaigns[-1]
    assert camp["campaign_type"] == "provider_promo"
    assert camp["provider"] == "FOX One"
    rec = sim.recommendation()
    assert rec["launchable"] is False
    live = rec["live"]
    assert live["id"] == camp["id"]
    assert live["n_targets"] == len(camp["targets"])
    assert live["days_left"] == camp["days_left"]
    assert live["start_day"] == camp["start_day"]


def test_scheduled_promo_does_not_block():
    sim = make_sim()
    sim.direct("starting day 20 for 7 days, " + promo_directive(sim))
    camp = sim.campaigns[-1]
    assert camp["campaign_type"] == "provider_promo"
    assert not sim._camp_active(camp)  # scheduled, not yet live
    rec = sim.recommendation()
    assert rec["launchable"] is True
    assert rec["live"] is None


def test_launchable_again_after_promo_expires():
    sim = make_sim()
    sim.direct(promo_directive(sim))
    camp = sim.campaigns[-1]
    assert sim.recommendation()["launchable"] is False
    expire(camp, sim)
    rec = sim.recommendation()
    assert rec["launchable"] is True
    assert rec["live"] is None


# ---------- directive parses to a provider_promo ----------

def test_directive_parses_to_provider_promo():
    sim = make_sim()
    parsed = sim.parse_directive(sim.recommendation()["directive"])
    assert parsed["action"] == "start"
    assert parsed["campaign_type"] == "provider_promo"
    assert parsed["provider"] == "FOX One"


def test_direct_with_directive_matches_manual_typing():
    sim = make_sim()
    directive = sim.recommendation()["directive"]
    # The Launch button's path: master-input filled, sent through sendMaster()
    # -> /api/direct -> sim.direct(). A second, identically seeded sim typing
    # the same text must produce an identical campaign.
    sim2 = make_sim()
    for s in (sim, sim2):
        r = s.direct(directive)
        assert r["ok"] is True
    c1, c2 = sim.campaigns[-1], sim2.campaigns[-1]
    for key in ("campaign_type", "provider", "genres", "days_left",
                "days_total", "start_day", "text"):
        assert c1[key] == c2[key], key
    assert c1["campaign_type"] == "provider_promo"
    assert c1["provider"] == "FOX One"
    assert c1["weight"] == 0.0  # a paid placement never steers taste
    assert c1["vec"] is None
    assert len(c1["targets"]) == len(c2["targets"]) > 0
