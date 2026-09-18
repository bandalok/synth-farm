"""synth-farm: a synthetic user farm for bootstrapping recommender systems.

Generate persona-driven behavioral data (searches, impressions, clicks,
plays, quartiles, completions, abandons) to train candidate-generation and
ranking models before you have real users.

Quickstart::

    python -m synth_farm --demo
"""

from .catalog import (
    ContentItem,
    PlatformAdapter,
    RankedItem,
    SyntheticCatalog,
    SyntheticPlatform,
)
from .config import Config, DEFAULT_GENRES
from .events import (
    EventSink,
    JSONLSink,
    MemorySink,
    SQLiteSink,
    load_events_jsonl,
    summarize,
    validate_event,
)
from .farm import Farm, FarmResult, RateLimitedAdapter
from .personas import (
    ARCHETYPES,
    Persona,
    archetype_distribution,
    generate_personas,
    population_report,
)
from .session import SessionEngine, click_probability, sample_session_times
from .training import (
    EvalMetrics,
    TrainingReport,
    als_fit,
    build_interaction_data,
    evaluate_held_out,
    train_and_evaluate,
)

__version__ = "0.1.0"

__all__ = [
    "ARCHETYPES",
    "DEFAULT_GENRES",
    "Config",
    "ContentItem",
    "EvalMetrics",
    "EventSink",
    "Farm",
    "FarmResult",
    "JSONLSink",
    "MemorySink",
    "Persona",
    "PlatformAdapter",
    "RankedItem",
    "RateLimitedAdapter",
    "SQLiteSink",
    "SessionEngine",
    "SyntheticCatalog",
    "SyntheticPlatform",
    "TrainingReport",
    "als_fit",
    "archetype_distribution",
    "build_interaction_data",
    "click_probability",
    "evaluate_held_out",
    "generate_personas",
    "load_events_jsonl",
    "population_report",
    "sample_session_times",
    "summarize",
    "train_and_evaluate",
    "validate_event",
]
