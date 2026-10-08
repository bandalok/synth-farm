"""Tests for configuration: defaults and environment overrides."""

from synth_farm.config import Config, DEFAULT_GENRES


def test_defaults_sane():
    cfg = Config()
    assert cfg.n_personas == 300
    assert cfg.position_bias_decay == 0.85
    assert len(cfg.genres) == 12
    assert cfg.genres == DEFAULT_GENRES


def test_from_env_overrides(monkeypatch):
    monkeypatch.setenv("SF_N_PERSONAS", "1000")
    monkeypatch.setenv("SF_POSITION_BIAS_DECAY", "0.5")
    monkeypatch.setenv("SF_SEED", "7")
    cfg = Config.from_env()
    assert cfg.n_personas == 1000
    assert cfg.position_bias_decay == 0.5
    assert cfg.seed == 7


def test_from_env_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("SF_N_PERSONAS", "not-a-number")
    cfg = Config.from_env()
    assert cfg.n_personas == 300


def test_from_env_genres_parsing(monkeypatch):
    monkeypatch.setenv("SF_GENRES", "action, comedy ,drama")
    cfg = Config.from_env()
    assert cfg.genres == ("action", "comedy", "drama")


def test_describe_roundtrip():
    cfg = Config()
    d = cfg.describe()
    assert d["n_personas"] == 300
    assert isinstance(d["genres"], list)
