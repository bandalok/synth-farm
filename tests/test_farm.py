"""Tests for the async farm runner."""

import asyncio

import numpy as np

from synth_farm import events as ev
from synth_farm.catalog import SyntheticCatalog, SyntheticPlatform
from synth_farm.farm import Farm, RateLimitedAdapter
from synth_farm.personas import generate_personas


def _farm(genres, config, n_personas, seed):
    personas = generate_personas(n_personas, genres, seed=seed)
    catalog = SyntheticCatalog(genres, size=config.catalog_size, seed=7)
    platform = SyntheticPlatform(catalog, seed=11)
    sink = ev.MemorySink()
    return Farm(config, personas, platform, sink, seed=seed + 5), sink


def _canon(events):
    keys = []
    for e in events:
        keys.append((
            e["persona_id"], e["session_id"], e["type"], e["ts"],
            e.get("item_id"), e.get("query"), e.get("quartile"),
            e.get("rank"), e.get("watch_fraction"),
        ))
    return sorted(keys)


def test_farm_runs_and_reports(genres, small_config):
    farm, sink = _farm(genres, small_config, n_personas=12, seed=900)
    result = asyncio.run(farm.run(days=3, progress=False))
    assert result.personas_run == 12
    assert result.sessions_run > 0
    assert result.events_emitted > 0
    assert result.elapsed_s > 0
    assert sum(result.events_by_type.values()) == result.events_emitted
    assert "click" in result.events_by_type
    assert "Farm run complete" in result.report()


def test_farm_events_all_synthetic(genres, small_config):
    farm, sink = _farm(genres, small_config, n_personas=10, seed=901)
    asyncio.run(farm.run(days=3, progress=False))
    assert sink.events
    assert all(e["synthetic"] is True for e in sink.events)


def test_farm_is_deterministic_up_to_ordering(genres, small_config):
    def run_once():
        farm, sink = _farm(genres, small_config, n_personas=15, seed=902)
        asyncio.run(farm.run(days=4, progress=False))
        return sink.events

    a, b = run_once(), run_once()
    assert len(a) == len(b) > 0
    assert _canon(a) == _canon(b)


def test_farm_stop_before_start_runs_nothing(genres, small_config):
    farm, sink = _farm(genres, small_config, n_personas=10, seed=903)
    farm.stop()
    result = asyncio.run(farm.run(days=3, progress=False))
    assert result.sessions_run == 0
    assert result.events_emitted == 0


def test_rate_limited_adapter_passes_through(platform):
    adapter = RateLimitedAdapter(platform, max_inflight=4)

    async def go():
        recs = await adapter.recommend("u1", n=5)
        assert len(recs) == 5
        res = await adapter.search("comedy", "u1", limit=3)
        assert len(res) <= 3
        await adapter.record_event(ev.make_search("u1", "s", "q", 1, "sl"))
        assert adapter.inner.recorded

    asyncio.run(go())


def test_farm_scales_to_hundred_agents(genres, small_config):
    # Not the full 1000 (CI time), but proves the machinery scales.
    small_config.progress_every_n_events = 10**9
    farm, sink = _farm(genres, small_config, n_personas=100, seed=904)
    result = asyncio.run(farm.run(days=2, progress=False))
    assert result.personas_run == 100
    assert result.events_emitted > 1000
    assert result.elapsed_s < 120


def test_constructs_after_loop_cleared_py39_style(genres, small_config, monkeypatch):
    """Regression: on Python 3.9, asyncio.Semaphore()/Event() grab the event
    loop eagerly at construction, which raises "no current event loop" once
    an earlier asyncio.run() has cleared the policy's loop. Farm pieces must
    construct lazily so the suite passes on 3.9+."""
    import asyncio.events

    real_sem, real_event = asyncio.Semaphore, asyncio.Event

    class StrictSemaphore(real_sem):
        def __init__(self, *a, **k):
            # Mimic 3.9's eager get_event_loop() at construction.
            asyncio.events.get_event_loop_policy().get_event_loop()
            super().__init__(*a, **k)

    class StrictEvent(real_event):
        def __init__(self, *a, **k):
            asyncio.events.get_event_loop_policy().get_event_loop()
            super().__init__(*a, **k)

    monkeypatch.setattr(asyncio, "Semaphore", StrictSemaphore)
    monkeypatch.setattr(asyncio, "Event", StrictEvent)

    # Poison the policy exactly the way a completed asyncio.run() does.
    asyncio.run(asyncio.sleep(0))

    farm, sink = _farm(genres, small_config, n_personas=10, seed=777)
    result = asyncio.run(farm.run(days=2, progress=False))
    assert result.sessions_run > 0
    assert result.events_emitted > 0
    assert sink.events
