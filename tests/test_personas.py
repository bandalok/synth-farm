"""Tests for persona generation: determinism, shapes, archetype behavior."""

import numpy as np

from synth_farm.personas import (
    ARCHETYPES,
    archetype_distribution,
    generate_personas,
    population_report,
)


def test_archetype_weights_sum_to_one():
    assert abs(sum(a.weight for a in ARCHETYPES) - 1.0) < 1e-9
    assert len(ARCHETYPES) == 6


def test_generate_is_deterministic(genres):
    a = generate_personas(50, genres, seed=99)
    b = generate_personas(50, genres, seed=99)
    assert [p.persona_id for p in a] == [p.persona_id for p in b]
    assert [p.archetype for p in a] == [p.archetype for p in b]
    for pa, pb in zip(a, b):
        assert np.array_equal(pa.taste, pb.taste)
        assert pa.sessions_per_week == pb.sessions_per_week
        assert pa.search_propensity == pb.search_propensity


def test_different_seeds_differ(genres):
    a = generate_personas(50, genres, seed=1)
    b = generate_personas(50, genres, seed=2)
    assert not np.array_equal(a[0].taste, b[0].taste)


def test_taste_vectors_are_distributions(genres):
    personas = generate_personas(100, genres, seed=5)
    for p in personas:
        assert p.taste.shape == (len(genres),)
        assert abs(p.taste.sum() - 1.0) < 1e-9
        assert (p.taste >= 0).all()


def test_propensions_in_unit_interval(genres):
    personas = generate_personas(100, genres, seed=6)
    for p in personas:
        assert 0.0 <= p.search_propensity <= 1.0
        assert 0.0 <= p.clickiness <= 1.0
        assert 0.0 <= p.completion_propensity <= 1.0
        assert p.sessions_per_week >= 0


def test_archetype_proportions_match_weights(genres):
    personas = generate_personas(6000, genres, seed=7)
    dist = archetype_distribution(personas)
    assert abs(sum(dist.values()) - 1.0) < 1e-9
    for arch in ARCHETYPES:
        assert abs(dist[arch.name] - arch.weight) < 0.03, arch.name


def test_loyalists_are_peakier_than_explorers(genres):
    personas = generate_personas(600, genres, seed=8)
    loyal = [p.taste.max() for p in personas if p.archetype == "genre_loyalist"]
    explor = [p.taste.max() for p in personas if p.archetype == "explorer"]
    assert loyal and explor
    assert float(np.mean(loyal)) > float(np.mean(explor)) + 0.15


def test_top_genres_sorted_descending(genres):
    personas = generate_personas(20, genres, seed=9)
    for p in personas[:5]:
        top = p.top_genres(3)
        assert len(top) == 3
        weights = [w for _, w in top]
        assert weights == sorted(weights, reverse=True)


def test_sample_units_bounded(genres):
    rng = np.random.default_rng(10)
    personas = generate_personas(50, genres, seed=11)
    for p in personas:
        for _ in range(20):
            u = p.sample_units(rng, cap=3)
            assert 1 <= u <= 3


def test_population_report_mentions_archetypes(genres):
    personas = generate_personas(50, genres, seed=12)
    report = population_report(personas)
    for arch in ARCHETYPES:
        assert arch.name in report


def test_persona_ids_unique(genres):
    personas = generate_personas(500, genres, seed=13)
    ids = [p.persona_id for p in personas]
    assert len(set(ids)) == len(ids)


def test_binge_watchers_more_active_than_casuals(genres):
    personas = generate_personas(2000, genres, seed=14)
    binge = [p.sessions_per_week for p in personas if p.archetype == "binge_watcher"]
    casual = [p.sessions_per_week for p in personas if p.archetype == "casual"]
    assert float(np.mean(binge)) > float(np.mean(casual))
