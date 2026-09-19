"""Real content catalog backed by an offline TMDb fixture.

The farm's viewers, sessions, and events are always synthetic — this
module only swaps *what* they watch: real trending titles (with real
genres, years, posters, and per-app streaming availability) instead of
the built-in synthetic catalog.

Data flow, deliberately boring:

1. ``~/workspace/skills/tmdb/bin/tmdb.py build-catalog`` pulls trending
   movies + TV and their US subscription-streaming providers, and writes
   an offline JSON fixture. That is the only step that touches the
   network, and it happens once, by hand.
2. ``load_real_catalog`` reads the fixture and returns ``RealCatalog``,
   whose items are shaped exactly like ``catalog.ContentItem`` (same
   ``item_id`` / ``title`` / ``genre_vector`` / ``popularity`` /
   ``duration_min`` / ``year`` / ``primary_genre`` attributes) plus
   TMDb extras: ``tmdb_id``, ``media_type``, ``poster_path``,
   ``providers``. Anything that consumes a ``SyntheticCatalog``
   (``collections.score_titles``, ``build_home_screen``,
   ``SyntheticPlatform``) works on it unchanged.
3. Tests and demos always use the committed fixture — never the network.

Attribution (required by the TMDb terms of use):
"This product uses the TMDb API but is not endorsed or certified by TMDb."
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

import numpy as np

from .apps import APPS
from .catalog import ContentItem, RankedItem, _tokenize
from .config import DEFAULT_GENRES

#: Where the committed offline fixture lives, relative to the package.
FIXTURE_PATH = Path(__file__).resolve().parent.parent / "data" / "catalog_tmdb.json"

#: TMDb genre id -> farm genre. Movies and TV share most ids; the
#: tv-only ids are mapped to their nearest farm genre. Every known id is
#: covered; anything unknown is counted, not silently dropped.
TMDB_GENRE_MAP: dict[int, str] = {
    28: "action",      # Action
    12: "action",      # Adventure
    16: "animation",   # Animation
    35: "comedy",      # Comedy
    80: "crime",       # Crime
    99: "documentary",  # Documentary
    18: "drama",       # Drama
    10751: "animation",  # Family
    14: "fantasy",     # Fantasy
    36: "drama",       # History -> drama
    27: "horror",      # Horror
    10402: "documentary",  # Music -> documentary
    9648: "thriller",  # Mystery -> thriller
    10749: "romance",  # Romance
    878: "scifi",      # Science Fiction
    53: "thriller",    # Thriller
    10752: "drama",    # War -> drama
    37: "action",      # Western -> action
    # TV-only ids:
    10759: "action",      # Action & Adventure
    10762: "animation",   # Kids
    10763: "documentary",  # News
    10764: "reality",     # Reality
    10765: "scifi",       # Sci-Fi & Fantasy
    10766: "drama",       # Soap
    10767: "documentary",  # Talk
    10768: "drama",       # War & Politics
    10770: "drama",       # TV Movie
}

#: TMDb watch-provider names -> farm app names. Names are TMDb's
#: display strings, so this maps the fixture's actual vocabulary
#: (verified Sep 18, 2026). Provider names that are not one of the
#: platform's apps (fuboTV, Crunchyroll, Starz, ...) are dropped from
#: ``providers`` and counted in ``RealCatalog.stats``.
PROVIDER_APP_MAP: dict[str, str] = {
    "Netflix": "Netflix",
    "Netflix Standard with Ads": "Netflix",
    "Disney Plus": "Disney+",
    "Max": "HBO Max",
    "HBO Max": "HBO Max",
    "HBO Max Amazon Channel": "HBO Max",
    "Hulu": "Hulu",
    "Amazon Prime Video": "Prime Video",
    "Amazon Prime Video with Ads": "Prime Video",
    "Apple TV": "Apple TV+",
    "Apple TV Plus": "Apple TV+",
    "Apple TV+": "Apple TV+",
    "Apple TV Amazon Channel": "Apple TV+",
    "Peacock": "Peacock",
    "Peacock Premium": "Peacock",
    "Peacock Premium Plus": "Peacock",
    "Paramount Plus": "Paramount+",
    "Paramount Plus Premium": "Paramount+",
    "Paramount Plus Essential": "Paramount+",
    "Paramount Plus Apple TV channel": "Paramount+",
    "Paramount+ Amazon Channel": "Paramount+",
    "Paramount+ Roku Premium Channel": "Paramount+",
    "Tubi TV": "Tubi",
    "Plex": "Plex",
}

#: Estimated runtimes. The fixture carries no runtimes, and the session
#: engine ticks its clock off ``duration_min`` — 0 would freeze simulated
#: time — so movies and episodes get honest, documented estimates.
_MOVIE_MINUTES = 105
_TV_MINUTES = 45


@dataclass
class RealContentItem(ContentItem):
    """A ``ContentItem`` with real TMDb metadata attached."""

    tmdb_id: int = 0
    media_type: str = ""  # "movie" | "tv"
    poster_path: str = ""  # "/abc123.jpg" -> https://image.tmdb.org/t/p/w342/...
    providers: tuple[str, ...] = field(default_factory=tuple)  # farm app names

    @property
    def poster_url(self, size: str = "w342") -> str:
        return (
            f"https://image.tmdb.org/t/p/{size}{self.poster_path}"
            if self.poster_path
            else ""
        )


class RealCatalog:
    """Offline real-title catalog. Same shape as ``SyntheticCatalog``.

    ``items`` / ``by_id`` / ``genres`` mirror the synthetic catalog's
    retrieval surface, so farm, platform, and collections code needs no
    changes. Item order is deterministic: the fixture is written sorted
    by (media_type, tmdb_id).
    """

    def __init__(
        self,
        genres: Sequence[str] = DEFAULT_GENRES,
        path: str | Path = FIXTURE_PATH,
    ) -> None:
        self.genres = tuple(genres)
        self.path = Path(path)
        self.items, self.stats = self._build()
        self.by_id: dict[str, RealContentItem] = {
            i.item_id: i for i in self.items
        }

    # -- construction ------------------------------------------------------
    def _build(self) -> tuple[list[RealContentItem], dict]:
        raw = json.loads(self.path.read_text())
        entries = raw["items"]
        n_genres = len(self.genres)
        items: list[RealContentItem] = []
        max_pop = max((float(e.get("popularity") or 0.0) for e in entries), default=1.0) or 1.0
        unmapped_genres: set[int] = set()
        unmapped_providers: set[str] = set()
        titles_without_providers = 0

        for e in entries:
            farm_genres: list[str] = []
            for gid in e.get("genre_ids", []):
                mapped = TMDB_GENRE_MAP.get(gid)
                if mapped is None:
                    unmapped_genres.add(gid)
                elif mapped not in farm_genres:
                    farm_genres.append(mapped)
            if not farm_genres:
                # No usable genre info: fall back to drama rather than
                # inventing a vector. Counted, never silent.
                farm_genres = ["drama"]

            vec = np.zeros(n_genres)
            for g in farm_genres:
                vec[self.genres.index(g)] = 1.0 / len(farm_genres)

            providers: list[str] = []
            for pname in e.get("providers_flatrate", []):
                app = PROVIDER_APP_MAP.get(pname)
                if app is None:
                    unmapped_providers.add(pname)
                elif app not in providers:
                    providers.append(app)
            if not providers:
                titles_without_providers += 1

            media = e.get("media_type", "")
            tid = int(e["tmdb_id"])
            release = e.get("release_date") or ""
            year = int(release[:4]) if len(release) >= 4 and release[:4].isdigit() else 0
            items.append(
                RealContentItem(
                    item_id=f"tmdb-{media}-{tid}",
                    title=str(e.get("title") or f"Untitled {tid}"),
                    genre_vector=vec,
                    popularity=float(e.get("popularity") or 0.0) / max_pop,
                    duration_min=_MOVIE_MINUTES if media == "movie" else _TV_MINUTES,
                    year=year,
                    primary_genre=farm_genres[0],
                    tmdb_id=tid,
                    media_type=media,
                    poster_path=str(e.get("poster_path") or ""),
                    providers=tuple(providers),
                )
            )

        stats = {
            "titles": len(items),
            "unmapped_genre_ids": sorted(unmapped_genres),
            "unmapped_providers": sorted(unmapped_providers),
            "titles_without_providers": titles_without_providers,
        }
        return items, stats

    # -- retrieval (mirrors SyntheticCatalog) --------------------------------
    def search(self, query: str, limit: int = 10) -> list[RankedItem]:
        """Keyword search over titles + genre terms, popularity as tiebreak.

        Same contract as ``SyntheticCatalog.search`` so ``SyntheticPlatform``
        works unchanged on the real catalog.
        """
        tokens = _tokenize(query)
        if not tokens:
            return []
        scored: list[tuple[float, RealContentItem]] = []
        for item in self.items:
            title_tokens = set(_tokenize(item.title))
            score = 0.0
            for tok in tokens:
                if tok in title_tokens:
                    score += 2.0
                elif any(tok == gt for gt in _tokenize(item.primary_genre)):
                    score += 1.0 * float(item.genre_vector.max())
                elif any(t.startswith(tok) and len(tok) >= 4 for t in title_tokens):
                    score += 0.8
            if score > 0:
                scored.append((score + 0.15 * item.popularity, item))
        scored.sort(key=lambda s: s[0], reverse=True)
        return [
            RankedItem(item=item, score=score, rank=r)
            for r, (score, item) in enumerate(scored[:limit])
        ]

    def genre_of(self, item: ContentItem) -> str:
        return self.genres[int(np.argmax(item.genre_vector))]


def load_real_catalog(
    genres: Sequence[str] = DEFAULT_GENRES,
    path: str | Path = FIXTURE_PATH,
) -> RealCatalog:
    """Load the offline TMDb fixture. No network, fully deterministic."""
    return RealCatalog(genres=genres, path=path)


def catalog_kind_names() -> tuple[str, ...]:
    return ("synthetic", "real")


__all__ = [
    "TMDB_GENRE_MAP",
    "PROVIDER_APP_MAP",
    "RealContentItem",
    "RealCatalog",
    "load_real_catalog",
    "FIXTURE_PATH",
]
