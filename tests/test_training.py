"""Tests for the training loop: matrix building, ALS, held-out eval."""

import asyncio

import numpy as np

from synth_farm import events as ev
from synth_farm.catalog import SyntheticCatalog, SyntheticPlatform
from synth_farm.farm import Farm
from synth_farm.personas import generate_personas
from synth_farm.training import (
    EVENT_WEIGHTS,
    als_fit,
    build_interaction_data,
    fold_in,
    train_and_evaluate,
)


def _hand_events():
    base = dict(synthetic=True, session_id="s", ts="2026-09-14T10:00:00+00:00")
    return [
        {**base, "event_id": "e1", "type": "click", "persona_id": "u1",
         "slate_id": "sl", "rank": 0, "item_id": "m1"},
        {**base, "event_id": "e2", "type": "play", "persona_id": "u1",
         "item_id": "m1", "duration_min": 60},
        {**base, "event_id": "e3", "type": "complete", "persona_id": "u1",
         "item_id": "m1", "watch_fraction": 1.0},
        {**base, "event_id": "e4", "type": "click", "persona_id": "u2",
         "slate_id": "sl", "rank": 3, "item_id": "m2"},
    ]


def test_build_interaction_data_weights():
    data = build_interaction_data(
        _hand_events(), ["u1", "u2"], ["m1", "m2"], alpha=8.0
    )
    assert data.n_interactions == 2
    u1 = data.user_index["u1"]
    items, conf = data.positives[u1]
    assert items.tolist() == [data.item_index["m1"]]
    # click(1) + play(2) + complete(5) = 8 -> conf = 1 + 8*8 = 65
    assert conf[0] == 1.0 + 8.0 * 8.0
    u2 = data.user_index["u2"]
    _, conf2 = data.positives[u2]
    assert conf2[0] == 1.0 + 8.0 * 1.0


def test_build_interaction_data_ignores_non_feedback():
    e = ev.make_search("u1", "s", "horror", 5, "sl")
    data = build_interaction_data([e], ["u1"], ["m1"], alpha=8.0)
    assert data.n_interactions == 0
    assert data.positives == {}


def test_als_loss_decreases():
    rng = np.random.default_rng(0)
    n_users, n_items, f = 25, 40, 6
    # Block-structured synthetic interactions: users like items in "their" block.
    events = []
    for u in range(n_users):
        block = (u * 4) % n_items
        for j in range(block, block + 6):
            events.append({
                "event_id": f"e{u}-{j}", "type": "click", "synthetic": True,
                "persona_id": f"u{u}", "session_id": "s",
                "ts": "2026-09-14T10:00:00+00:00",
                "slate_id": "sl", "rank": 0, "item_id": f"m{j % n_items}",
            })
    data = build_interaction_data(
        events, [f"u{u}" for u in range(n_users)],
        [f"m{j}" for j in range(n_items)], alpha=8.0,
    )
    _, _, losses = als_fit(data, n_factors=f, n_iter=8, reg=0.1, rng=rng)
    assert len(losses) == 8
    assert losses[-1] < losses[0]


def test_fold_in_shape():
    rng = np.random.default_rng(1)
    Y = rng.normal(size=(50, 8))
    x = fold_in(np.array([1, 5, 9]), np.array([9.0, 9.0, 9.0]), Y, reg=0.1)
    assert x.shape == (8,)


def _run_small_farm(genres, config, n_personas, days, seed):
    catalog = SyntheticCatalog(genres, size=config.catalog_size, seed=seed + 1)
    personas = generate_personas(n_personas, genres, seed=seed)
    platform = SyntheticPlatform(catalog, seed=seed + 2)
    sink = ev.MemorySink()
    farm = Farm(config, personas, platform, sink, seed=seed + 3)
    asyncio.run(farm.run(days=days, progress=False))
    return sink.events, personas, catalog


def test_end_to_end_recall_beats_random(genres):
    # Demo-scale end to end: the farm must generate behavior a model can
    # actually learn from. ~10s; this is the test that matters.
    from synth_farm.config import Config
    cfg = Config()
    cfg.progress_every_n_events = 10**9
    events, personas, catalog = _run_small_farm(
        genres, cfg, n_personas=300, days=7, seed=4242
    )
    assert len(events) > 10000
    report = train_and_evaluate(
        events, personas, catalog, cfg, seed=4243
    )
    m = report.metrics
    assert m is not None
    assert m.n_eval_users >= 5
    assert m.lift_vs_random > 3.0, (
        f"lift {m.lift_vs_random:.1f}x is not clearly above random "
        f"(recall {m.mean_recall_at_k:.3f} vs {m.random_baseline:.3f})"
    )
    assert report.als_losses[-1] <= report.als_losses[0]
    assert "recall@" in report.report()


def test_training_report_samples(genres, small_config):
    small_config.als_factors = 8
    small_config.als_iterations = 3
    events, personas, catalog = _run_small_farm(
        genres, small_config, n_personas=100, days=7, seed=4343
    )
    report = train_and_evaluate(
        events, personas, catalog, small_config, seed=4344
    )
    assert report.samples, "expected sample recommendation lines"
    assert any("recommends:" in s for s in report.samples)
