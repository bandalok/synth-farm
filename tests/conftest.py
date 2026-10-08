"""Shared fixtures for the synth-farm test suite."""

import numpy as np
import pytest

from synth_farm.catalog import SyntheticCatalog, SyntheticPlatform
from synth_farm.config import Config, DEFAULT_GENRES
from synth_farm import events as ev
from synth_farm.personas import generate_personas
from synth_farm.session import SessionEngine


@pytest.fixture()
def genres():
    return list(DEFAULT_GENRES)


@pytest.fixture()
def small_config():
    cfg = Config.from_env()
    cfg.catalog_size = 120
    cfg.n_personas = 40
    cfg.sim_days = 4
    cfg.als_factors = 8
    cfg.als_iterations = 4
    cfg.eval_top_k = 10
    cfg.slate_size = 8
    cfg.search_result_limit = 6
    cfg.progress_every_n_events = 10**9  # silence progress in tests
    return cfg


@pytest.fixture()
def catalog(genres, small_config):
    return SyntheticCatalog(genres, size=small_config.catalog_size, seed=7)


@pytest.fixture()
def platform(catalog):
    return SyntheticPlatform(catalog, seed=11)


@pytest.fixture()
def personas(genres):
    return generate_personas(40, genres, seed=42)


@pytest.fixture()
def engine(small_config, platform):
    return SessionEngine(small_config, platform, ev.MemorySink())


@pytest.fixture()
def rng():
    return np.random.default_rng(1234)
