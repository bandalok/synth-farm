"""Tests for the Britbox app launch: promo-only title pool.

Britbox's 500 TMDB titles live in data/catalog_britbox.json, loaded as a
sidecar promo catalog. They must NEVER appear in rails, search, or cold
start — they surface solely through Britbox app-promotion campaigns.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "live_demo"))
from server import LiveSim  # noqa: E402
from synth_farm.apps import APPS, APP_POPULARITY  # noqa: E402

REPO = os.path.join(os.path.dirname(__file__), "..")
BRITBOX_FIXTURE = os.path.join(REPO, "data", "catalog_britbox.json")

needs_fixture = pytest.mark.skipif(
    not os.path.exists(BRITBOX_FIXTURE),
    reason="catalog_britbox.json not pulled yet")


@pytest.fixture(scope="module")
def sim():
    s = LiveSim(n_agents=60, seed=21, tick_seconds=0.05)
    for _ in range(3):
        with s.lock:
            s.tick()
    return s


def test_britbox_in_apps():
    assert "Britbox" in APPS
    idx = APPS.index("Britbox")
    assert APP_POPULARITY[idx] > 0
    assert len(APPS) == len(APP_POPULARITY)


@needs_fixture
def test_promo_catalog_loads(sim):
    import json
    n = len(json.load(open(BRITBOX_FIXTURE))["items"])
    assert len(sim.promo_by_id) == n >= 490
    for it in sim.promo_by_id.values():
        assert "Britbox" in it.providers


@needs_fixture
def test_promo_exclusive_titles_stay_promo_only(sim):
    # Titles also in the main catalog are regular titles; the promo-only
    # guarantee covers titles exclusive to the Britbox fixture.
    exclusive = set(sim.promo_by_id) - set(sim.by_id)
    assert exclusive, "expected some Britbox-exclusive titles"
    p = sim.personas[0]
    hs = sim.home_screen(p)
    rail_ids = {d.get("id") for r in hs["rails"] for d in r["items"]}
    assert not (rail_ids & exclusive)


@needs_fixture
def test_britbox_absent_from_search(sim):
    # Titles also in the main catalog are regular titles; the promo-only
    # guarantee covers titles exclusive to the Britbox fixture (same
    # carve-out as test_promo_exclusive_titles_stay_promo_only). The main
    # catalog grew 700->8643 and now shares 105 TMDB titles with the promo
    # fixture, so sampling must come from the exclusive set.
    exclusive = set(sim.promo_by_id) - set(sim.by_id)
    assert exclusive, "expected some Britbox-exclusive titles"
    sample = next(it for it in sim.promo_by_id.values() if it.item_id in exclusive)
    res = sim.search(sample.title.split()[0][:6] or "the")
    assert all(r["id"] not in exclusive for r in res)


@needs_fixture
def test_britbox_absent_from_home_rails(sim):
    exclusive = set(sim.promo_by_id) - set(sim.by_id)
    p = sim.personas[0]
    hs = sim.home_screen(p)
    rail_ids = {d.get("id") for r in hs["rails"] for d in r["items"]}
    assert not (rail_ids & exclusive)


@needs_fixture
def test_britbox_app_campaign_pins_promo_titles(sim):
    r = sim.start_app_campaign("Britbox", coverage=0.5, days=7)
    assert r["ok"]
    camp = sim.campaigns[-1]
    try:
        assert camp["campaign_type"] == "provider_promo"
        assert camp["provider"] == "Britbox"
        assert set(camp["titles"]) == set(sim.promo_by_id) | {
            tid for tid in sim.by_id if "Britbox" in sim.by_id[tid].providers}
        assert camp["titles"], "Britbox campaign needs pinned titles"
        # targets are non-subscribers
        for i in camp["targets"]:
            assert "Britbox" not in sim.personas[i].subscribed_apps
        # the promo rail renders for a targeted agent
        pi = next(iter(camp["targets"]))
        home = sim.home_screen(sim.personas[pi])
        rail = next((r for r in home["rails"]
                     if r.get("kind") == "provider_promo"), None)
        assert rail is not None
        assert rail["title"] == "Trending on Britbox"
        assert rail["items"], "promo rail must carry Britbox titles"
        for d in rail["items"]:
            assert d["id"] in sim.promo_by_id
    finally:
        sim.campaigns.remove(camp)


@needs_fixture
def test_britbox_title_click_converts(sim):
    r = sim.start_app_campaign("Britbox", coverage=1.0, days=7)
    camp = sim.campaigns[-1]
    try:
        pi = next(iter(camp["targets"]))
        p = sim.personas[pi]
        tid = next(iter(camp["titles"]))
        # the click path must resolve the promo-only title without error
        out = sim.click(p.persona_id, tid)
        assert out["id"] == p.persona_id
        evs = sim.agent_events[p.persona_id]
        assert evs[-1]["item_id"] == tid
        assert evs[-1]["type"] == "play"
    finally:
        sim.campaigns.remove(camp)
