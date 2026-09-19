"""Command-line interface for the synthetic user farm.

Usage::

    python -m synth_farm --demo
        One command, no API keys: 300 personas x 7 simulated days against
        the built-in synthetic catalog, then ALS training + held-out
        evaluation with a printed metrics report.

    python -m synth_farm run --personas 1000 --days 7 --seed 42
        Full farm run. Writes events to events.jsonl (and optionally a
        SQLite db with --db).

    python -m synth_farm train --events events.jsonl [--seed 42 ...]
        Train ALS on an events file and print the evaluation report.
        Personas and the catalog are regenerated deterministically from
        --seed, so pass the same seed you ran with.

    python -m synth_farm collections --personas 500 --days 7 --seed 7
        Run the farm, then print each persona cluster's CTV home-screen
        collections: top picks, trending now, continue watching.
        Add --catalog real to rank real TMDb titles instead of synthetic ones
        (viewers and events stay synthetic either way).

All behavior is also tunable via SF_* environment variables (see
.env.example and config.py).
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from . import events as ev
from .catalog import SyntheticCatalog, SyntheticPlatform
from .collections import ARCHETYPE_NAMES, build_home_screen, pretty_name
from .config import Config
from .farm import Farm
from .personas import generate_personas, population_report
from .real_catalog import catalog_kind_names, load_real_catalog
from .training import train_and_evaluate


def _build_world(config: Config, catalog_kind: str = "synthetic"):
    """Personas + catalog + platform for a run. Deterministic from seed.

    ``catalog_kind`` is "synthetic" (built-in fake titles) or "real"
    (offline TMDb fixture — real titles, synthetic viewers either way).
    """
    if catalog_kind == "real":
        catalog = load_real_catalog(config.genres)
    elif catalog_kind == "synthetic":
        catalog = SyntheticCatalog(
            config.genres, size=config.catalog_size, seed=config.seed + 1
        )
    else:
        raise ValueError(f"unknown catalog kind: {catalog_kind!r}")
    personas = generate_personas(
        config.n_personas, config.genres, seed=config.seed
    )
    platform = SyntheticPlatform(catalog, seed=config.seed + 2)
    return catalog, personas, platform


async def cmd_demo(args: argparse.Namespace) -> int:
    config = Config.from_env()
    config.n_personas = args.personas
    config.sim_days = args.days
    if args.seed is not None:
        config.seed = args.seed

    print("=== synth-farm demo ===")
    print(f"config: {config.n_personas} personas x {config.sim_days} days, "
          f"seed={config.seed}, catalog={config.catalog_size} items")
    catalog, personas, platform = _build_world(config)
    print(population_report(personas))
    print(f"catalog: {len(catalog.items)} synthetic titles "
          f"across {len(config.genres)} genres")

    sink = ev.MemorySink()
    farm = Farm(config, personas, platform, sink, seed=config.seed + 3)
    result = await farm.run(progress=True)
    print(result.report())

    print()
    report = train_and_evaluate(
        sink.events, personas, catalog, config, seed=config.seed + 4
    )
    print(report.report())
    print("\nDemo complete. Every event above has synthetic=true.")
    return 0


async def cmd_run(args: argparse.Namespace) -> int:
    config = Config.from_env()
    config.n_personas = args.personas
    config.sim_days = args.days
    if args.seed is not None:
        config.seed = args.seed

    print("=== synth-farm run ===")
    print(f"config: {config.n_personas} personas x {config.sim_days} days, "
          f"seed={config.seed}")
    catalog, personas, platform = _build_world(config)
    print(population_report(personas))

    sink: ev.EventSink = ev.JSONLSink(args.events)
    if args.db:
        # Tee into SQLite as well when --db is given.
        sqlite_sink = ev.SQLiteSink(args.db)

        class _Tee(ev.EventSink):
            def write(self, event):  # type: ignore[override]
                sink.write(event)
                sqlite_sink.write(event)

            def close(self):
                sink.close()
                sqlite_sink.close()

        active: ev.EventSink = _Tee()
    else:
        active = sink

    farm = Farm(config, personas, platform, active, seed=config.seed + 3)
    try:
        result = await farm.run(progress=True)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\nInterrupted — shutting down agents gracefully…")
        farm.stop()
        raise
    print(result.report())
    print(f"events written to: {args.events}")
    if args.db:
        print(f"sqlite db written to: {args.db}")
    return 0


def cmd_train(args: argparse.Namespace) -> int:
    config = Config.from_env()
    config.n_personas = args.personas
    if args.seed is not None:
        config.seed = args.seed
    if args.catalog_size is not None:
        config.catalog_size = args.catalog_size

    print("=== synth-farm train ===")
    print(f"loading events from {args.events} …")
    events = ev.load_events_jsonl(args.events)
    print(f"loaded {len(events):,} events")
    summary = ev.summarize(events)
    print(f"personas in file: {summary['personas']}, "
          f"sessions: {summary['sessions']}")

    # Personas + catalog are deterministic functions of the seed, so the
    # train/eval split reproduces the original run's population exactly.
    catalog, personas, _ = _build_world(config)
    report = train_and_evaluate(
        events, personas, catalog, config, seed=config.seed + 4
    )
    print()
    print(report.report())
    return 0


async def cmd_collections(args: argparse.Namespace) -> int:
    config = Config.from_env()
    config.n_personas = args.personas
    config.sim_days = args.days
    if args.seed is not None:
        config.seed = args.seed

    print("=== synth-farm collections ===")
    print(f"config: {config.n_personas} personas x {config.sim_days} days, "
          f"seed={config.seed}, catalog={args.catalog}")
    catalog, personas, platform = _build_world(config, catalog_kind=args.catalog)
    n_titles = len(catalog.items)
    kind_word = "real TMDb titles" if args.catalog == "real" else "synthetic titles"
    print(f"catalog: {n_titles} {kind_word} "
          f"across {len(config.genres)} genres")

    sink = ev.MemorySink()
    farm = Farm(config, personas, platform, sink, seed=config.seed + 3)
    await farm.run(progress=True)
    events = sink.events
    print(f"{len(events):,} events\n")

    for archetype in ARCHETYPE_NAMES:
        screen = build_home_screen(
            archetype, personas, catalog, events, n=args.rail_length
        )
        print(f"##### {pretty_name(archetype)} — home screen #####")
        for rail, items in screen.items():
            print(f"== {rail} ==")
            for i, item in enumerate(items, 1):
                print(f"  {i}. {item.title} ({item.primary_genre})")
            if not items:
                print("  (empty)")
        print()
    print("Collections complete. " +
          ("Every title above is a real TMDb title; viewers and events are synthetic."
           if args.catalog == "real" else
           "Every title above is synthetic."))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m synth_farm",
        description="Synthetic user farm: behavioral data to bootstrap "
                    "recommender systems.",
    )
    parser.add_argument(
        "--demo", action="store_true",
        help="one-command offline demo (300 personas x 7 days + training)",
    )
    sub = parser.add_subparsers(dest="command")

    p_run = sub.add_parser("run", help="run the full farm, write events")
    p_run.add_argument("--personas", type=int, default=1000)
    p_run.add_argument("--days", type=int, default=7)
    p_run.add_argument("--seed", type=int, default=None)
    p_run.add_argument("--events", default="events.jsonl",
                       help="JSONL output path")
    p_run.add_argument("--db", default=None,
                       help="optional SQLite output path")

    p_train = sub.add_parser("train", help="train ALS on an events file")
    p_train.add_argument("--events", required=True)
    p_train.add_argument("--seed", type=int, default=None,
                         help="seed used for the run (to rebuild personas)")
    p_train.add_argument("--personas", type=int, default=1000,
                         help="persona count used for the run")
    p_train.add_argument("--catalog-size", type=int, default=None)

    p_col = sub.add_parser("collections",
                           help="per-cluster home-screen collections")
    p_col.add_argument("--personas", type=int, default=500)
    p_col.add_argument("--days", type=int, default=7)
    p_col.add_argument("--seed", type=int, default=None)
    p_col.add_argument("--rail-length", type=int, default=6,
                       help="titles per rail")
    p_col.add_argument("--catalog", choices=catalog_kind_names(),
                       default="synthetic",
                       help="synthetic titles (default) or real TMDb titles")

    # Demo flags (also work as: python -m synth_farm --demo --personas 100 …)
    parser.add_argument("--personas", type=int, default=300,
                        help="(demo) persona count")
    parser.add_argument("--days", type=int, default=7, help="(demo) sim days")
    parser.add_argument("--seed", type=int, default=None, help="(demo) seed")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.demo or args.command is None:
        return asyncio.run(cmd_demo(args))
    if args.command == "run":
        return asyncio.run(cmd_run(args))
    if args.command == "train":
        return cmd_train(args)
    if args.command == "collections":
        return asyncio.run(cmd_collections(args))
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
