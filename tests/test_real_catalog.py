"""Tests for the real TMDb catalog. Offline only: the committed fixture,
never the network."""

import numpy as np
import pytest

from synth_farm.apps import APPS
from synth_farm.collections import build_home_screen, score_titles
from synth_farm.config import DEFAULT_GENRES
from synth_farm.real_catalog import (
    PROVIDER_APP_MAP,
    TMDB_GENRE_MAP,
    RealCatalog,
    load_real_catalog,
)


@pytest.fixture(scope="module")
def catalog():
    return load_real_catalog()


def test_fixture_loads_with_plenty_of_titles(catalog):
    assert len(catalog.items) > 50
    assert catalog.stats["titles"] == len(catalog.items)


def test_genre_vectors_are_distributions(catalog):
    for item in catalog.items:
        assert abs(float(item.genre_vector.sum()) - 1.0) < 1e-9
        assert (item.genre_vector >= 0).all()
        assert item.primary_genre in DEFAULT_GENRES


def test_every_provider_maps_to_a_known_app(catalog):
    assert "Tubi" in APPS  # the free tier is a first-class app
    for item in catalog.items:
        for app in item.providers:
            assert app in APPS, f"{item.title}: {app} not a known app"
    # And the map itself only ever points at known apps.
    assert set(PROVIDER_APP_MAP.values()) <= set(APPS)


def test_item_ids_are_unique_and_tmdb_shaped(catalog):
    ids = [i.item_id for i in catalog.items]
    assert len(set(ids)) == len(ids)
    assert all(i.startswith("tmdb-") for i in ids)
    assert len(catalog.by_id) == len(ids)


def test_genre_map_covers_all_standard_tmdb_ids():
    # The 19 canonical movie ids + 8 tv-only ids. Nothing may fall
    # through silently.
    movie_ids = [28, 12, 16, 35, 80, 99, 18, 10751, 14, 36, 27,
                 10402, 9648, 10749, 878, 53, 10752, 37]
    tv_ids = [10759, 10762, 10763, 10764, 10765, 10766, 10767, 10768]
    for gid in movie_ids + tv_ids:
        assert gid in TMDB_GENRE_MAP
        assert TMDB_GENRE_MAP[gid] in DEFAULT_GENRES


def test_no_unmapped_genre_ids_in_fixture(catalog):
    assert catalog.stats["unmapped_genre_ids"] == []


def test_score_titles_ranks_horror_first_for_horror_centroid(catalog):
    centroid = np.zeros(len(DEFAULT_GENRES))
    centroid[DEFAULT_GENRES.index("horror")] = 1.0
    ranked = score_titles(centroid, catalog)
    horror = [i for i in catalog.items if i.primary_genre == "horror"]
    assert horror, "fixture should contain horror titles"
    top5 = [item for item, _ in ranked[:5]]
    assert sum(1 for i in top5 if i.primary_genre == "horror") >= 3


def test_real_catalog_search_finds_titles(catalog):
    from synth_farm.catalog import RankedItem

    results = catalog.search("spider", limit=5)
    assert results and all(isinstance(r, RankedItem) for r in results)
    assert any("spider" in r.item.title.lower() for r in results)
    assert catalog.search("") == []


def test_build_home_screen_end_to_end_on_real_catalog(genres, small_config):
    import asyncio

    from synth_farm import events as ev
    from synth_farm.catalog import SyntheticPlatform
    from synth_farm.farm import Farm
    from synth_farm.personas import generate_personas

    catalog = load_real_catalog(genres)
    personas = generate_personas(12, genres, seed=4242)
    platform = SyntheticPlatform(catalog, seed=11)
    sink = ev.MemorySink()
    farm = Farm(small_config, personas, platform, sink, seed=4243)
    asyncio.run(farm.run(days=2, progress=False))

    screen = build_home_screen("genre_loyalist", personas, catalog,
                               sink.events, n=4)
    assert list(screen) == [
        "Top picks for Genre Loyalists",
        "Trending now",
        "Continue watching",
    ]
    assert len(screen["Top picks for Genre Loyalists"]) == 4
    # Every event still claims to be synthetic, even with real titles.
    assert all(e["synthetic"] is True for e in sink.events)
    play_ids = {e["item_id"] for e in sink.events if e["type"] == "play"}
    assert play_ids <= set(catalog.by_id), "plays reference real catalog ids"


def test_fixture_is_deterministic():
    a = load_real_catalog()
    b = load_real_catalog()
    assert [i.item_id for i in a.items] == [i.item_id for i in b.items]
    assert [i.title for i in a.items] == [i.title for i in b.items]
