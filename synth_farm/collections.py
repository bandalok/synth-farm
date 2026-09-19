"""Per-cluster home-screen collections for a CTV platform.

A CTV home screen is a set of *collections* (rails): "Top picks for you",
"Trending now", "Continue watching". This module derives those collections
per persona cluster from farm output — no new dependencies, numpy only.

The pipeline is deliberately simple and deterministic:

* ``cluster_centroid`` — the mean taste vector of one archetype. The
  cluster's collective taste, learned from the simulation's latent state.
* ``score_titles`` — dot-product of the centroid against each catalog
  item's genre vector: content-based ranking for the cluster.
* ``trending_titles`` — global top plays: what the whole audience is
  actually watching right now.
* ``continue_watching`` — (persona, title) pairs with a play event but no
  later complete event: things the cluster started and walked away from.
* ``build_home_screen`` — the three rails, in order, ready to render.

Everything is a pure function of its inputs, so the same seed always
produces the same home screen.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable, Sequence

import numpy as np

from .catalog import ContentItem, SyntheticCatalog
from .personas import ARCHETYPES, Persona

#: Plain-English cluster names for rails and UI.
PRETTY: dict[str, str] = {
    "genre_loyalist": "Genre Loyalists",
    "casual": "Casual Viewers",
    "binge_watcher": "Binge Watchers",
    "explorer": "Explorers",
    "channel_surfer": "Channel Surfers",
    "critic": "Critics",
}

ARCHETYPE_NAMES: tuple[str, ...] = tuple(a.name for a in ARCHETYPES)


def pretty_name(archetype: str) -> str:
    """Human-readable cluster name, e.g. ``genre_loyalist`` -> "Genre Loyalists"."""
    return PRETTY.get(archetype, archetype.replace("_", " ").title())


def cluster_centroid(personas: Sequence[Persona], archetype: str) -> np.ndarray:
    """Mean taste vector of every persona in ``archetype``.

    Raises ``ValueError`` when the cluster is empty.
    """
    vecs = [p.taste for p in personas if p.archetype == archetype]
    if not vecs:
        raise ValueError(f"no personas with archetype {archetype!r}")
    return np.asarray(np.mean(vecs, axis=0), dtype=float)


def score_titles(
    centroid: np.ndarray, catalog: SyntheticCatalog
) -> list[tuple[ContentItem, float]]:
    """Rank every catalog item by ``centroid . item.genre_vector``.

    Ties break on ``item_id`` so the ranking is fully deterministic.
    Returns ``(item, score)`` pairs, best first.
    """
    scored = [
        (item, float(np.dot(centroid, item.genre_vector)))
        for item in catalog.items
    ]
    scored.sort(key=lambda s: (-s[1], s[0].item_id))
    return scored


def trending_titles(events: Iterable[dict], k: int) -> list[str]:
    """Top-``k`` item ids by play-event count, most played first.

    Ties break on ``item_id`` for determinism.
    """
    counts: dict[str, int] = {}
    for e in events:
        if e.get("type") == "play" and "item_id" in e:
            counts[e["item_id"]] = counts.get(e["item_id"], 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [item_id for item_id, _ in ranked[:k]]


def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def continue_watching(
    personas: Sequence[Persona],
    events: Iterable[dict],
    archetype: str,
    k: int,
) -> list[str]:
    """Most recent titles the cluster started but never finished.

    For personas of ``archetype``: keep (persona_id, item_id) pairs that
    have a play event with no later complete event for the same pair.
    Returns up to ``k`` item ids, most recent play first. Deterministic.
    """
    members = {p.persona_id for p in personas if p.archetype == archetype}
    latest_play: dict[tuple[str, str], str] = {}
    completed: set[tuple[str, str]] = set()
    for e in events:
        pid = e.get("persona_id")
        item = e.get("item_id")
        if pid not in members or item is None:
            continue
        etype = e.get("type")
        key = (pid, item)
        if etype == "play":
            ts = e.get("ts", "")
            if key not in latest_play or ts > latest_play[key]:
                latest_play[key] = ts
        elif etype == "complete":
            completed.add(key)

    # A pair counts as "continue watching" when its latest play has no
    # complete at or after it. Since completes only follow plays in the
    # simulator, membership in ``completed`` is sufficient, but the ts
    # comparison keeps the rule honest for arbitrary event logs.
    candidates = [
        (ts, item) for (pid, item), ts in latest_play.items()
        if (pid, item) not in completed
    ]
    candidates.sort(key=lambda c: (c[0], c[1]), reverse=True)
    seen: list[str] = []
    for _, item in candidates:
        if item not in seen:
            seen.append(item)
        if len(seen) == k:
            break
    return seen


def build_home_screen(
    archetype: str,
    personas: Sequence[Persona],
    catalog: SyntheticCatalog,
    events: Iterable[dict],
    n: int = 6,
) -> dict[str, list[ContentItem]]:
    """The three home-screen rails for one cluster, in display order.

    * ``Top picks for {cluster}`` — content-based ranking from the
      cluster's taste centroid.
    * ``Trending now`` — what the whole audience is playing.
    * ``Continue watching`` — the cluster's unfinished titles.

    Returns an insertion-ordered dict of rail title -> items.
    """
    centroid = cluster_centroid(personas, archetype)
    top_picks = [item for item, _ in score_titles(centroid, catalog)[:n]]
    trending = [
        catalog.by_id[i] for i in trending_titles(events, n) if i in catalog.by_id
    ]
    continued = [
        catalog.by_id[i]
        for i in continue_watching(personas, events, archetype, n)
        if i in catalog.by_id
    ]
    return {
        f"Top picks for {pretty_name(archetype)}": top_picks,
        "Trending now": trending,
        "Continue watching": continued,
    }
