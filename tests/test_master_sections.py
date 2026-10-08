"""Tests for the Master Agent sub-sections: collections management,
apps campaigns, calendar tentpoles, featured row campaigns,
search boost campaigns, and screen takeover campaigns."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "live_demo"))
from server import LiveSim, RAIL_LABELS, US_TENTPOLES  # noqa: E402
from synth_farm.apps import APPS  # noqa: E402


@pytest.fixture(scope="module")
def sim():
    s = LiveSim(n_agents=60, seed=11, tick_seconds=0.05)
    for _ in range(3):
        with s.lock:
            s.tick()
    return s


def _agent0(sim):
    return sim.personas[0]


# ---------- collections management ----------

def test_rail_config_hide(sim):
    sim.rail_config = {"hidden": ["trending"], "order": []}
    try:
        hs = sim.home_screen(_agent0(sim))
        kinds = [r["kind"] for r in hs["rails"]]
        assert "trending" not in kinds
    finally:
        sim.rail_config = {"hidden": [], "order": []}


def test_rail_config_custom_order(sim):
    sim.rail_config = {"hidden": [], "order": ["continue", "featured", "apps"]}
    try:
        hs = sim.home_screen(_agent0(sim))
        kinds = [r["kind"] for r in hs["rails"]]
        assert kinds.index("continue") < kinds.index("featured")
        assert kinds.index("featured") < kinds.index("apps")
    finally:
        sim.rail_config = {"hidden": [], "order": []}


def test_rail_config_reset_restores_pins(sim):
    sim.rail_config = {"hidden": [], "order": []}
    hs = sim.home_screen(_agent0(sim))
    kinds = [r["kind"] for r in hs["rails"]]
    assert kinds[0] == "featured" and kinds[1] == "apps" and kinds[2] == "continue"


def test_rail_labels_cover_known_kinds():
    for k in ("featured", "apps", "continue", "trending", "new", "gems"):
        assert k in RAIL_LABELS


# ---------- apps campaign ----------

def test_app_campaign_creates_promo(sim):
    before = len(sim.campaigns)
    r = sim.start_app_campaign("Netflix", coverage=0.5, days=7)
    assert r["ok"]
    camp = sim.campaigns[-1]
    assert camp["campaign_type"] == "provider_promo"
    assert camp["provider"] == "Netflix"
    assert len(sim.campaigns) == before + 1
    # targets must not already subscribe to Netflix
    for i in camp["targets"]:
        assert "Netflix" not in sim.personas[i].subscribed_apps


def test_app_campaign_unknown_app_rejected(sim):
    r = sim.start_app_campaign("NotARealApp", coverage=0.5)
    assert not r["ok"]


def test_apps_endpoint_lists_farm_apps():
    assert "Netflix" in APPS and "Crunchyroll" in APPS


# ---------- calendar ----------

def test_tentpoles_list():
    assert len(US_TENTPOLES) >= 8
    names = [n for n, _, _ in US_TENTPOLES]
    assert any("Super Bowl" in n for n in names)
    assert any("Thanksgiving" in n for n in names)


# ---------- featured row campaign ----------

def test_featured_campaign_pins_titles(sim):
    target_titles = [it.item_id for it in sim.catalog.items[:3]]
    r = sim.start_featured_campaign(target_titles, coverage=1.0, days=7)
    assert r["ok"]
    camp = sim.campaigns[-1]
    assert camp["campaign_type"] == "featured"
    try:
        hs = sim.home_screen(_agent0(sim))
        feat = next(r for r in hs["rails"] if r["kind"] == "featured")
        feat_ids = [it["id"] for it in feat["items"]]
        for tid in target_titles:
            assert tid in feat_ids
        # pinned titles lead the rail
        assert set(feat_ids[:3]) == set(target_titles)
    finally:
        sim.campaigns.remove(camp)


def test_featured_campaign_rejects_bad_titles(sim):
    r = sim.start_featured_campaign(["nope-not-real"], coverage=0.5)
    assert not r["ok"]


# ---------- search boost campaign ----------

def test_search_boost_leads_results(sim):
    pick = sim.catalog.items[5]
    r = sim.start_search_campaign([pick.item_id], coverage=1.0, days=7)
    assert r["ok"]
    camp = sim.campaigns[-1]
    try:
        res = sim.search(pick.title.split()[0], _agent0(sim))
        assert res, "expected search results"
        assert res[0]["id"] == pick.item_id
        assert res[0].get("promoted") is True
    finally:
        sim.campaigns.remove(camp)


def test_search_boost_not_shown_to_untargeted(sim):
    pick = sim.catalog.items[6]
    r = sim.start_search_campaign([pick.item_id], coverage=0.0, days=7)
    camp = sim.campaigns[-1]
    try:
        assert len(camp["targets"]) >= 1
        # an untargeted agent: find one outside targets
        outsider = next(i for i in range(sim.n_agents)
                        if i not in camp["targets"])
        pid = sim.personas[outsider].persona_id
        res = sim.search(pick.title.split()[0], pid)
        boosted = [x for x in res if x["id"] == pick.item_id
                   and x.get("promoted")]
        assert not boosted
    finally:
        sim.campaigns.remove(camp)


# ---------- screen takeover campaign ----------

def test_takeover_payload_in_home_screen(sim):
    pick = sim.catalog.items[7]
    r = sim.start_takeover_campaign(pick.item_id, "Don't miss it!",
                                    coverage=1.0, days=7)
    assert r["ok"]
    camp = sim.campaigns[-1]
    assert camp["campaign_type"] == "takeover"
    try:
        hs = sim.home_screen(_agent0(sim))
        to = hs.get("takeover")
        assert to is not None
        assert to["id"] == pick.item_id
        assert to["headline"] == "Don't miss it!"
    finally:
        sim.campaigns.remove(camp)


def test_takeover_absent_for_untargeted(sim):
    pick = sim.catalog.items[8]
    r = sim.start_takeover_campaign(pick.item_id, "Hi", coverage=0.0, days=7)
    camp = sim.campaigns[-1]
    try:
        outsider = next(i for i in range(sim.n_agents)
                        if i not in camp["targets"])
        hs = sim.home_screen(sim.personas[outsider])
        assert hs.get("takeover") is None
    finally:
        sim.campaigns.remove(camp)


# ---------- campaign json carries new types ----------

def test_campaigns_json_includes_new_types(sim):
    r = sim.start_search_campaign([sim.catalog.items[9].item_id],
                                  coverage=0.1, days=3)
    camp = sim.campaigns[-1]
    try:
        js = sim._campaigns_json()
        row = next(c for c in js if c["id"] == camp["id"])
        assert row["campaign_type"] == "search_boost"
        assert row["titles"]
        labels = sim._campaign_target_labels(camp)
        assert labels == ["search boost"]
    finally:
        sim.campaigns.remove(camp)
