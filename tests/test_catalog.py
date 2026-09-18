"""Tests for the synthetic catalog and platform adapters."""

import asyncio

import numpy as np
import pytest

from synth_farm.catalog import SyntheticCatalog, SyntheticPlatform


def test_catalog_is_deterministic(genres):
    a = SyntheticCatalog(genres, size=100, seed=3)
    b = SyntheticCatalog(genres, size=100, seed=3)
    assert [i.title for i in a.items] == [i.title for i in b.items]
    assert [i.item_id for i in a.items] == [i.item_id for i in b.items]


def test_catalog_titles_unique(genres):
    cat = SyntheticCatalog(genres, size=400, seed=4)
    titles = [i.title for i in cat.items]
    assert len(set(titles)) == len(titles)


def test_genre_vectors_are_distributions(catalog, genres):
    for item in catalog.items:
        assert item.genre_vector.shape == (len(genres),)
        assert abs(item.genre_vector.sum() - 1.0) < 1e-9
        assert (item.genre_vector >= 0).all()


def test_popularity_in_unit_interval(catalog):
    pops = [i.popularity for i in catalog.items]
    assert min(pops) >= 0.0 and max(pops) <= 1.0
    assert max(pops) == 1.0  # normalized so the top hit is 1.0


def test_search_genre_term_returns_that_genre(catalog):
    results = catalog.search("horror", limit=10)
    assert len(results) > 0
    assert all(r.item.primary_genre == "horror" for r in results)


def test_search_title_fragment_finds_item(catalog):
    target = catalog.items[10]
    token = target.title.split()[0].lower()
    results = catalog.search(token, limit=10)
    assert any(r.item.item_id == target.item_id for r in results)


def test_search_empty_query_returns_nothing(catalog):
    assert catalog.search("", limit=10) == []
    assert catalog.search("zzzqqqxx", limit=10) == []


def test_search_ranks_are_sequential(catalog):
    results = catalog.search("comedy", limit=8)
    assert [r.rank for r in results] == list(range(len(results)))


def test_recommend_cold_user_is_popularity_ordered(platform, catalog):
    recs = asyncio.run(platform.recommend("brand-new-user", n=10))
    assert len(recs) == 10
    assert [r.rank for r in recs] == list(range(10))
    pops = [r.item.popularity for r in recs]
    assert pops == sorted(pops, reverse=True)


def test_recommend_follows_history_when_popularity_off(genres):
    catalog = SyntheticCatalog(genres, size=200, seed=21)
    platform = SyntheticPlatform(catalog, seed=22, popularity_weight=0.0)
    horror_ids = [i.item_id for i in catalog.items
                  if i.primary_genre == "horror"][:5]
    for iid in horror_ids:
        platform.note_interaction("horror-fan", iid)
    recs = asyncio.run(platform.recommend("horror-fan", n=10))
    top_genres = [r.item.primary_genre for r in recs[:3]]
    assert all(g == "horror" for g in top_genres)


def test_record_event_tracks_history(platform, catalog):
    event = {
        "event_id": "e1", "type": "click", "synthetic": True,
        "persona_id": "persona-00001", "session_id": "s1",
        "ts": "2026-09-14T10:00:00+00:00",
        "slate_id": "sl1", "rank": 0, "item_id": catalog.items[0].item_id,
    }
    asyncio.run(platform.record_event(event))
    assert platform.recorded[-1]["event_id"] == "e1"
    assert platform._history["persona-00001"] == [catalog.items[0].item_id]


def test_intent_sampling_follows_taste(genres):
    catalog = SyntheticCatalog(genres, size=300, seed=30)
    platform = SyntheticPlatform(catalog, seed=31)
    rng = np.random.default_rng(32)
    taste = np.zeros(len(genres))
    taste[genres.index("scifi")] = 0.9
    taste += 0.1 / len(genres)
    taste /= taste.sum()
    picks = [platform.sample_intent_item(taste, rng) for _ in range(400)]
    scifi_share = sum(1 for p in picks if p.primary_genre == "scifi") / 400
    assert scifi_share > 0.5


def test_adapter_is_abstract():
    from synth_farm.catalog import PlatformAdapter
    with pytest.raises(TypeError):
        PlatformAdapter()  # type: ignore[abstract]
