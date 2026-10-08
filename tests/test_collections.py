"""Tests for per-cluster home-screen collections."""

import asyncio

import numpy as np
import pytest

from synth_farm import events as ev
from synth_farm.catalog import SyntheticCatalog, SyntheticPlatform
from synth_farm.collections import (
    ARCHETYPE_NAMES,
    build_home_screen,
    cluster_centroid,
    continue_watching,
    pretty_name,
    score_titles,
    trending_titles,
)
from synth_farm.config import DEFAULT_GENRES
from synth_farm.farm import Farm
from synth_farm.personas import generate_personas


def _world(n_personas=40, seed=4242):
    genres = list(DEFAULT_GENRES)
    personas = generate_personas(n_personas, genres, seed=seed)
    catalog = SyntheticCatalog(genres, size=120, seed=seed + 1)
    return personas, catalog


def _play(pid, item, ts):
    return {"type": "play", "synthetic": True, "persona_id": pid,
            "session_id": "s1", "item_id": item, "ts": ts,
            "duration_min": 45}


def _complete(pid, item, ts):
    return {"type": "complete", "synthetic": True, "persona_id": pid,
            "session_id": "s1", "item_id": item, "ts": ts,
            "watch_fraction": 1.0}


def test_cluster_centroid_is_mean_of_member_tastes():
    personas, _ = _world()
    members = [p for p in personas if p.archetype == "genre_loyalist"]
    assert members, "expected some genre loyalists in the population"
    got = cluster_centroid(personas, "genre_loyalist")
    want = np.mean([p.taste for p in members], axis=0)
    assert np.allclose(got, want)


def test_cluster_centroid_empty_cluster_raises():
    personas, _ = _world()
    others = [p for p in personas if p.archetype != "critic"]
    with pytest.raises(ValueError):
        cluster_centroid(others, "critic")


def test_score_titles_sorted_desc_and_deterministic():
    _, catalog = _world()
    centroid = np.ones(12) / 12
    a = score_titles(centroid, catalog)
    b = score_titles(centroid, catalog)
    assert [i.item_id for i, _ in a] == [i.item_id for i, _ in b]
    scores = [s for _, s in a]
    assert scores == sorted(scores, reverse=True)
    assert len(a) == len(catalog.items)


def test_score_titles_aligns_with_centroid_genre():
    _, catalog = _world()
    genres = catalog.genres
    drama = genres.index("drama")
    centroid = np.zeros(len(genres))
    centroid[drama] = 1.0
    n_drama = sum(1 for i in catalog.items if i.primary_genre == "drama")
    assert n_drama > 0
    top = score_titles(centroid, catalog)[:n_drama]
    assert all(item.primary_genre == "drama" for item, _ in top)


def test_trending_titles_counts_plays_only():
    events = [
        _play("p1", "a", "2026-09-10T10:00:00+00:00"),
        _play("p2", "a", "2026-09-10T11:00:00+00:00"),
        _play("p1", "b", "2026-09-10T12:00:00+00:00"),
        _complete("p1", "a", "2026-09-10T13:00:00+00:00"),
        {"type": "click", "persona_id": "p1", "session_id": "s",
         "item_id": "c", "ts": "2026-09-10T14:00:00+00:00"},
    ]
    assert trending_titles(events, 2) == ["a", "b"]


def test_continue_watching_excludes_completed_pairs():
    personas, _ = _world()
    loyal = [p.persona_id for p in personas
             if p.archetype == "genre_loyalist"][:2]
    p1, p2 = loyal[0], loyal[1]
    events = [
        _play(p1, "item-a", "2026-09-10T10:00:00+00:00"),
        _complete(p1, "item-a", "2026-09-10T12:00:00+00:00"),  # finished: out
        _play(p1, "item-b", "2026-09-10T13:00:00+00:00"),      # unfinished
        _play(p2, "item-c", "2026-09-10T09:00:00+00:00"),      # unfinished, older
        _play("outsider", "item-d", "2026-09-10T15:00:00+00:00"),
    ]
    got = continue_watching(personas, events, "genre_loyalist", 6)
    assert got == ["item-b", "item-c"]
    assert "item-d" not in got


def test_continue_watching_respects_cluster_membership():
    personas, _ = _world()
    events = [_play("no-such-persona", "item-x",
                    "2026-09-10T10:00:00+00:00")]
    assert continue_watching(personas, events, "casual", 6) == []


def test_build_home_screen_rails_in_order_and_deterministic(
        genres, small_config):
    personas = generate_personas(40, genres, seed=4242)
    catalog = SyntheticCatalog(genres, size=small_config.catalog_size, seed=7)
    platform = SyntheticPlatform(catalog, seed=11)
    sink = ev.MemorySink()
    farm = Farm(small_config, personas, platform, sink, seed=4242 + 5)
    asyncio.run(farm.run(days=3, progress=False))
    events = sink.events
    assert any(e["type"] == "play" for e in events)

    s1 = build_home_screen("casual", personas, catalog, events, n=6)
    s2 = build_home_screen("casual", personas, catalog, events, n=6)
    assert list(s1.keys()) == [
        "Top picks for Casual Viewers", "Trending now", "Continue watching"]
    assert all(len(items) > 0 for items in s1.values())
    assert len(s1["Top picks for Casual Viewers"]) == 6
    assert all(len(items) <= 6 for items in s1.values())
    assert [i.item_id for i in s1["Top picks for Casual Viewers"]] == \
           [i.item_id for i in s2["Top picks for Casual Viewers"]]


def test_top_picks_align_with_cluster_top_genre(genres):
    personas = generate_personas(60, genres, seed=4243)
    catalog = SyntheticCatalog(genres, size=200, seed=4244)
    centroid = cluster_centroid(personas, "genre_loyalist")
    top_genre = genres[int(np.argmax(centroid))]
    screen = build_home_screen("genre_loyalist", personas, catalog, [],
                               n=10)
    picks = screen["Top picks for Genre Loyalists"]
    matching = sum(1 for i in picks if i.primary_genre == top_genre)
    assert matching >= len(picks) // 2 + 1


def test_pretty_names_cover_all_archetypes():
    assert set(ARCHETYPE_NAMES) <= set(
        {"genre_loyalist", "casual", "binge_watcher", "explorer",
         "channel_surfer", "critic"})
    for name in ARCHETYPE_NAMES:
        assert pretty_name(name) and "_" not in pretty_name(name)
