"""Tests for the apps layer: subscriptions, affinity, app choice, app_launch."""

import asyncio
from collections import Counter

import numpy as np
import pytest

from synth_farm import events as ev
from synth_farm.apps import (
    APPS,
    APP_POPULARITY,
    choose_app,
    sample_app_affinity,
    sample_subscriptions,
)
from synth_farm.farm import Farm
from synth_farm.personas import generate_personas


def test_apps_catalog_has_popularity_weights():
    assert len(APPS) == len(APP_POPULARITY) == 11
    assert abs(sum(APP_POPULARITY) - 1.0) < 1e-9
    assert all(p > 0 for p in APP_POPULARITY)
    assert "Netflix" in APPS and "Disney+" in APPS and "HBO Max" in APPS
    assert "Tubi" in APPS  # free tier, added for the real TMDb catalog
    assert "Plex" in APPS  # free tier + personal media, added Sep 2026


def test_sample_subscriptions_is_deterministic(rng):
    a = sample_subscriptions(np.random.default_rng(99))
    b = sample_subscriptions(np.random.default_rng(99))
    assert a == b


def test_sample_subscriptions_nonempty_and_valid(rng):
    for seed in range(50):
        subs = sample_subscriptions(np.random.default_rng(seed))
        assert len(subs) >= 1, "every persona subscribes to at least one app"
        assert all(app in APPS for app in subs)
        assert len(set(subs)) == len(subs), "no duplicate subscriptions"


def test_sample_subscriptions_follows_popularity():
    counts = Counter()
    n = 2000
    for seed in range(n):
        for app in sample_subscriptions(np.random.default_rng(seed)):
            counts[app] += 1
    # Netflix (most popular) must beat Paramount+ (least popular) decisively.
    assert counts["Netflix"] > 2 * counts["Paramount+"]
    # Sanity: the average persona carries a few subscriptions.
    total = sum(counts.values())
    assert 2.0 < total / n < 4.5


def test_sample_app_affinity_sums_to_one(rng):
    subs = ("Netflix", "Hulu", "Peacock")
    aff = sample_app_affinity(rng, subs)
    assert len(aff) == len(subs)
    assert abs(aff.sum() - 1.0) < 1e-9
    assert (aff >= 0).all()


def test_sample_app_affinity_needs_subscribed(rng):
    with pytest.raises(ValueError):
        sample_app_affinity(rng, ())


def test_choose_app_only_returns_subscribed(rng):
    personas = generate_personas(30, ["drama", "comedy"], seed=7)
    for p in personas:
        for _ in range(20):
            assert choose_app(rng, p) in p.subscribed_apps


def test_choose_app_is_deterministic():
    personas = generate_personas(5, ["drama", "comedy"], seed=7)
    p = personas[0]
    a = [choose_app(np.random.default_rng(5), p) for _ in range(10)]
    b = [choose_app(np.random.default_rng(5), p) for _ in range(10)]
    assert a == b


def test_choose_app_fallback_for_persona_without_apps(rng):
    from synth_farm.personas import Persona

    p = Persona(
        persona_id="persona-x",
        archetype="casual",
        taste=np.array([0.5, 0.5]),
        sessions_per_week=2,
        search_propensity=0.3,
        clickiness=0.5,
        completion_propensity=0.5,
        mean_units=1.5,
        genres=("drama", "comedy"),
    )
    for _ in range(20):
        assert choose_app(rng, p) in APPS


def test_personas_carry_subscriptions_and_affinity(genres):
    personas = generate_personas(40, genres, seed=42)
    for p in personas:
        assert len(p.subscribed_apps) >= 1
        assert all(a in APPS for a in p.subscribed_apps)
        assert p.app_affinity is not None
        assert len(p.app_affinity) == len(p.subscribed_apps)
        assert abs(p.app_affinity.sum() - 1.0) < 1e-9


def test_app_launch_event_validates():
    e = ev.make_app_launch("persona-00001", "sess-1", "Netflix")
    assert ev.validate_event(e) is e
    assert e["synthetic"] is True
    assert e["type"] == "app_launch"
    assert e["app_name"] == "Netflix"


def test_app_launch_rejects_missing_app_name():
    e = ev.make_app_launch("p", "s", "Hulu")
    del e["app_name"]
    with pytest.raises(ev.EventValidationError):
        ev.validate_event(e)


def test_sessions_emit_app_launch_first(genres, small_config):
    from synth_farm.catalog import SyntheticCatalog, SyntheticPlatform
    from synth_farm.session import SessionEngine

    personas = generate_personas(6, genres, seed=77)
    catalog = SyntheticCatalog(genres, size=small_config.catalog_size, seed=7)
    platform = SyntheticPlatform(catalog, seed=11)
    sink = ev.MemorySink()
    engine = SessionEngine(small_config, platform, sink)
    rng = np.random.default_rng(5150)

    async def go():
        return await engine.run_session(
            personas[0], "sess-apps-001", 1, 20.5, rng
        )

    happened = asyncio.run(go())
    assert happened, "session emitted events"
    first = happened[0]
    assert first["type"] == "app_launch"
    assert first["app_name"] in APPS
    assert first["app_name"] in personas[0].subscribed_apps


def test_farm_sessions_each_have_exactly_one_app_launch(genres, small_config):
    from synth_farm.catalog import SyntheticCatalog, SyntheticPlatform

    personas = generate_personas(10, genres, seed=900)
    catalog = SyntheticCatalog(genres, size=small_config.catalog_size, seed=7)
    platform = SyntheticPlatform(catalog, seed=11)
    sink = ev.MemorySink()
    farm = Farm(small_config, personas, platform, sink, seed=905)
    result = asyncio.run(farm.run(days=3, progress=False))

    launches = [e for e in sink.events if e["type"] == "app_launch"]
    assert result.sessions_run > 0
    assert len(launches) == result.sessions_run
    assert all(e["synthetic"] is True for e in launches)
    assert set(e["app_name"] for e in launches) <= set(APPS)
