"""Central configuration for the synthetic user farm.

Every knob is an environment variable with a working default, so the demo
runs with zero setup and serious runs can be tuned without touching code.

The single most important design choice documented here: the click model
parameters (position-bias decay, click temperature) exist because a
recommender trained on synthetic data will inherit the simulator's
assumptions. Tune them deliberately and calibrate against real data as
soon as any exists.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_str(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


#: The default content taxonomy. Genres are the axes of every taste vector,
#: item vector, and diversity metric in the system.
DEFAULT_GENRES: tuple[str, ...] = (
    "action",
    "comedy",
    "drama",
    "horror",
    "scifi",
    "documentary",
    "romance",
    "thriller",
    "animation",
    "crime",
    "fantasy",
    "reality",
)


@dataclass
class Config:
    """All tunable parameters for persona generation, simulation, and training."""

    # -- Content taxonomy -------------------------------------------------
    genres: tuple[str, ...] = DEFAULT_GENRES
    catalog_size: int = 800  # items in the built-in synthetic catalog

    # -- Persona population ----------------------------------------------
    n_personas: int = 300
    seed: int = 42

    # -- Simulation window -------------------------------------------------
    sim_days: int = 7  # simulated days the farm runs over

    # -- Click model -------------------------------------------------------
    # P(click | rank) = clickiness * appeal**click_temperature * position_decay(rank)
    # appeal = 0.7 * relevance + 0.3 * popularity  (social proof matters)
    # position_decay(rank) = 1 / (1 + rank) ** position_bias_decay
    #
    # The decay is the load-bearing parameter: with decay = 0 every rank is
    # equally clickable and the trained ranker never learns that position
    # itself drives clicks. Real systems show strong decay; 0.7-1.0 is a
    # sane range for a video/content surface.
    position_bias_decay: float = 0.85
    click_temperature: float = 1.0  # >1 sharpens appeal differences

    # -- Session engine ----------------------------------------------------
    # Evening peak centered at 21:00, lunch bump at 12:30, dead zone 02:00-05:00.
    evening_peak_hour: float = 21.0
    evening_peak_width: float = 2.5  # stddev of the gaussian peak, in hours
    lunch_peak_hour: float = 12.5
    lunch_peak_weight: float = 0.45  # relative to the evening peak (=1.0)
    night_floor: float = 0.05  # minimum relative arrival rate (02:00-05:00)
    weekend_boost: float = 1.6  # Saturday/Sunday session multiplier

    slate_size: int = 12  # recommendation tiles shown per browse slate
    search_result_limit: int = 10
    max_units_per_session: int = 4  # cap on watch units per session
    typo_probability: float = 0.06  # chance a generated query has a typo

    # -- Farm runner ---------------------------------------------------------
    max_inflight_requests: int = 256  # global semaphore on platform calls
    agent_jitter_max_s: float = 0.0  # per-agent sleep before each session
    progress_every_n_events: int = 5000

    # -- Training --------------------------------------------------------------
    als_factors: int = 24
    als_iterations: int = 12
    als_regularization: float = 0.08
    als_confidence_alpha: float = 8.0  # confidence = 1 + alpha * strength
    train_fraction: float = 0.8  # fraction of personas used for training
    eval_top_k: int = 20  # K for recall@K

    # -- Outputs -----------------------------------------------------------------
    events_path: str = "events.jsonl"
    db_path: str = "synth_farm.db"
    briefs_dir: str = "briefs"

    @classmethod
    def from_env(cls) -> "Config":
        """Build a Config, letting environment variables override defaults."""
        genres_raw = _env_str("SF_GENRES", "")
        genres = (
            tuple(g.strip() for g in genres_raw.split(",") if g.strip())
            or DEFAULT_GENRES
        )
        return cls(
            genres=genres,
            catalog_size=_env_int("SF_CATALOG_SIZE", 800),
            n_personas=_env_int("SF_N_PERSONAS", 300),
            seed=_env_int("SF_SEED", 42),
            sim_days=_env_int("SF_SIM_DAYS", 7),
            position_bias_decay=_env_float("SF_POSITION_BIAS_DECAY", 0.85),
            click_temperature=_env_float("SF_CLICK_TEMPERATURE", 1.0),
            evening_peak_hour=_env_float("SF_EVENING_PEAK_HOUR", 21.0),
            evening_peak_width=_env_float("SF_EVENING_PEAK_WIDTH", 2.5),
            lunch_peak_hour=_env_float("SF_LUNCH_PEAK_HOUR", 12.5),
            lunch_peak_weight=_env_float("SF_LUNCH_PEAK_WEIGHT", 0.45),
            night_floor=_env_float("SF_NIGHT_FLOOR", 0.05),
            weekend_boost=_env_float("SF_WEEKEND_BOOST", 1.6),
            slate_size=_env_int("SF_SLATE_SIZE", 12),
            search_result_limit=_env_int("SF_SEARCH_RESULT_LIMIT", 10),
            max_units_per_session=_env_int("SF_MAX_UNITS_PER_SESSION", 4),
            typo_probability=_env_float("SF_TYPO_PROBABILITY", 0.06),
            max_inflight_requests=_env_int("SF_MAX_INFLIGHT", 256),
            agent_jitter_max_s=_env_float("SF_AGENT_JITTER_MAX_S", 0.0),
            progress_every_n_events=_env_int("SF_PROGRESS_EVERY", 5000),
            als_factors=_env_int("SF_ALS_FACTORS", 24),
            als_iterations=_env_int("SF_ALS_ITERATIONS", 12),
            als_regularization=_env_float("SF_ALS_REG", 0.08),
            als_confidence_alpha=_env_float("SF_ALS_ALPHA", 8.0),
            train_fraction=_env_float("SF_TRAIN_FRACTION", 0.8),
            eval_top_k=_env_int("SF_EVAL_TOP_K", 20),
            events_path=_env_str("SF_EVENTS_PATH", "events.jsonl"),
            db_path=_env_str("SF_DB_PATH", "synth_farm.db"),
            briefs_dir=_env_str("SF_BRIEFS_DIR", "briefs"),
        )

    def describe(self) -> dict:
        """Plain-dict view of the config, handy for run manifests."""
        return {
            "genres": list(self.genres),
            "catalog_size": self.catalog_size,
            "n_personas": self.n_personas,
            "seed": self.seed,
            "sim_days": self.sim_days,
            "position_bias_decay": self.position_bias_decay,
            "click_temperature": self.click_temperature,
            "slate_size": self.slate_size,
            "als_factors": self.als_factors,
            "als_iterations": self.als_iterations,
            "train_fraction": self.train_fraction,
            "eval_top_k": self.eval_top_k,
        }
