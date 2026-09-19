"""Content catalog and platform adapters.

Two things live here:

1. ``SyntheticCatalog`` — a fully self-contained fake catalog (titles,
   genre vectors, popularity, durations) so the farm, demo, and tests run
   with no real platform. Titles are assembled from per-genre word pools;
   every string in this file is original.
2. ``PlatformAdapter`` — the abstract boundary between the simulation and
   a real backend. Implement ``search`` / ``recommend`` / ``record_event``
   against your own APIs and the farm will drive real HTTP traffic shaped
   exactly like the synthetic path. ``SyntheticPlatform`` is the in-process
   reference implementation, with a popularity + content-similarity
   baseline recommender.

The baseline recommender deliberately only sees *behavioral history*
(clicked/watched item ids per user), never the persona's latent taste
vector. That separation is what makes the training loop honest: the model
has to infer taste from behavior, like a production system would.
"""

from __future__ import annotations

import math
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Sequence

import numpy as np


# ---------------------------------------------------------------------------
# Title word pools. (prefix, core) pairs per genre -> "Crimson Protocol".
# ---------------------------------------------------------------------------
_TITLE_POOLS: dict[str, tuple[list[str], list[str]]] = {
    "action": (
        ["Iron", "Steel", "Crimson", "Midnight", "Rogue", "Silent", "Final",
         "Broken", "Rapid", "Hollow", "Savage", "Lone"],
        ["Protocol", "Strike", "Vengeance", "Reckoning", "Siege", "Fury",
         "Thunder", "Assault", "Legacy", "Retaliation", "Uprising", "Valor"],
    ),
    "comedy": (
        ["Awkward", "Silly", "Clumsy", "Goofy", "Bumbling", "Wacky", "Sleepy",
         "Grumpy", "Sunny", "Lucky", "Dizzy", "Snappy"],
        ["Neighbors", "Wedding", "Roadtrip", "Reunion", "Diner", "Roommates",
         "Vacation", "Office", "Family", "Disaster", "Barbecue", "Karaoke"],
    ),
    "drama": (
        ["Quiet", "Last", "Broken", "Paper", "Winter", "Hollow", "Tender",
         "Long", "Fading", "Small", "Distant", "Patient"],
        ["Goodbye", "Promise", "Letters", "Homecoming", "Regret", "Harbor",
         "Echoes", "Seasons", "Forgiveness", "Meridian", "Vigil", "Crossing"],
    ),
    "horror": (
        ["Dark", "Whispering", "Cursed", "Bleeding", "Silent", "Forgotten",
         "Howling", "Pale", "Rotting", "Grim", "Veiled", "Sunken"],
        ["Manor", "Crypt", "Shadows", "Seance", "Graveyard", "Attic",
         "Possession", "Nightmare", "Coven", "Asylum", "Hollow", "Ritual"],
    ),
    "scifi": (
        ["Quantum", "Stellar", "Neon", "Orbital", "Chrome", "Void", "Solar",
         "Cyber", "Lunar", "Atomic", "Photon", "Drift"],
        ["Frontier", "Odyssey", "Uprising", "Signal", "Horizon", "Paradox",
         "Colony", "Empire", "Awakening", "Vector", "Relay", "Expanse"],
    ),
    "documentary": (
        ["Inside", "Chasing", "Wild", "The Last", "The Rise Of", "Planet",
         "Decoding", "The Secret Life Of"],
        ["Oceans", "Empires", "Innovators", "Wilderness", "Cities", "Chefs",
         "Explorers", "Dynasties", "Volcanoes", "Archives"],
    ),
    "romance": (
        ["Midnight", "Summer", "Parisian", "Tender", "Second", "Starlit",
         "Autumn", "Secret", "Paper", "Velvet"],
        ["Kiss", "Vows", "Serenade", "Rendezvous", "Hearts", "Letters",
         "Promise", "Waltz", "Confession", "Embrace"],
    ),
    "thriller": (
        ["Silent", "Deadly", "Hidden", "Twisted", "Vanishing", "Cold",
         "Double", "Broken", "Last", "Dark", "False", "Narrow"],
        ["Witness", "Alibi", "Conspiracy", "Deception", "Confession",
         "Suspect", "Trap", "Secret", "Motive", "Escape", "Cipher", "Fallout"],
    ),
    "animation": (
        ["Captain", "Princess", "Brave", "Magic", "Jolly", "Tiny", "Cosmic",
         "Sunny", "Peppy", "Wobbly"],
        ["Quest", "Kingdom", "Adventures", "Friends", "Voyage", "Dragon",
         "Island", "Express", "Bakery", "Circus"],
    ),
    "crime": (
        ["Dirty", "Cold", "Last", "Silent", "Crooked", "Broken", "Midnight",
         "Hard", "Big", "False"],
        ["Heist", "Syndicate", "Informant", "Precinct", "Alibi", "Undercover",
         "Getaway", "Kingpin", "Sting", "Ledger", "Wiretap", "Payoff"],
    ),
    "fantasy": (
        ["Dragon", "Elven", "Mystic", "Crystal", "Shadow", "Golden",
         "Ancient", "Enchanted", "Storm", "Ember"],
        ["Throne", "Prophecy", "Realm", "Sword", "Crown", "Sorcerer",
         "Kingdoms", "Spell", "Grove", "Oath"],
    ),
    "reality": (
        ["Extreme", "Ultimate", "Celebrity", "Island", "Kitchen", "Garage",
         "Dating", "Survival", "Desert", "AllStar"],
        ["Challenge", "Cookoff", "Makeover", "Race", "Showdown", "Wars",
         "Rescue", "Bakeoff", "Escape", "Trials"],
    ),
}

_WORD_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower())


@dataclass
class ContentItem:
    """One catalog entry."""

    item_id: str
    title: str
    genre_vector: np.ndarray  # sums to 1, over config.genres
    popularity: float  # 0..1, lognormal long tail
    duration_min: int
    year: int
    primary_genre: str = ""

    def describe(self, genres: Sequence[str]) -> str:
        top = genres[int(np.argmax(self.genre_vector))]
        return f"{self.title} [{top}] pop={self.popularity:.2f} {self.duration_min}min"


@dataclass
class RankedItem:
    """An item plus the score the platform assigned it, in slate order."""

    item: ContentItem
    score: float
    rank: int  # 0-based position in the slate


class SyntheticCatalog:
    """Generates a deterministic fake catalog of ``size`` items."""

    def __init__(
        self,
        genres: Sequence[str],
        size: int = 800,
        seed: int = 7,
    ) -> None:
        self.genres = tuple(genres)
        self.size = size
        self.seed = seed
        self.items: list[ContentItem] = self._build()
        self.by_id: dict[str, ContentItem] = {i.item_id: i for i in self.items}

    # -- construction ------------------------------------------------------
    def _build(self) -> list[ContentItem]:
        rng = np.random.default_rng(self.seed)
        n_genres = len(self.genres)
        items: list[ContentItem] = []
        used_titles: set[str] = set()
        # Lognormal popularity: a few hits, a long tail. Normalized to 0..1.
        raw_pop = rng.lognormal(mean=0.0, sigma=1.1, size=self.size)
        raw_pop = raw_pop / raw_pop.max()

        for i in range(self.size):
            g = int(rng.integers(n_genres))
            genre = self.genres[g]
            title = self._unique_title(genre, rng, used_titles)

            # Genre vector: dominant primary genre + optional secondary blend.
            vec = np.zeros(n_genres)
            primary_w = float(rng.uniform(0.72, 0.96))
            vec[g] = primary_w
            if rng.random() < 0.35:
                g2 = int(rng.integers(n_genres))
                if g2 != g:
                    vec[g2] = 1.0 - primary_w
                else:
                    vec[g] = 1.0
            else:
                vec[g] = 1.0
            vec = vec / vec.sum()

            items.append(
                ContentItem(
                    item_id=f"item-{i:05d}",
                    title=title,
                    genre_vector=vec,
                    popularity=float(raw_pop[i]),
                    duration_min=int(rng.integers(22, 181)),
                    year=int(rng.integers(1998, 2027)),
                    primary_genre=genre,
                )
            )
        return items

    def _unique_title(
        self, genre: str, rng: np.random.Generator, used: set[str]
    ) -> str:
        prefixes, cores = _TITLE_POOLS.get(genre, (["Untitled"], ["Story"]))
        for _ in range(50):
            title = f"{rng.choice(prefixes)} {rng.choice(cores)}"
            if rng.random() < 0.18 and not title.startswith("The "):
                title = "The " + title
            if title not in used:
                used.add(title)
                return title
        # Extremely unlikely fallback: numbered variant.
        n = 2
        while f"{title} {n}" in used:
            n += 1
        used.add(f"{title} {n}")
        return f"{title} {n}"

    # -- retrieval -----------------------------------------------------------
    def search(self, query: str, limit: int = 10) -> list[RankedItem]:
        """Keyword search over titles + genre terms, popularity as tiebreak."""
        tokens = _tokenize(query)
        if not tokens:
            return []
        scored: list[tuple[float, ContentItem]] = []
        for item in self.items:
            title_tokens = set(_tokenize(item.title))
            score = 0.0
            for tok in tokens:
                if tok in title_tokens:
                    score += 2.0
                elif any(tok == gt for gt in _tokenize(item.primary_genre)):
                    score += 1.0 * float(item.genre_vector.max())
                # prefix match: "quant" finds "quantum"
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


# ---------------------------------------------------------------------------
# Platform adapter boundary
# ---------------------------------------------------------------------------
class PlatformAdapter(ABC):
    """The seam between the farm and a real backend.

    Implement these three methods against your own services and the farm
    will drive them exactly the way it drives the synthetic platform:

    * ``search``    — full-text / semantic search over your catalog.
    * ``recommend`` — your recommender's slate for a user (tiles in order).
    * ``record_event`` — ingest one interaction event (impression, click...).

    All methods are async so real implementations can do HTTP with
    retries/backoff; the in-process synthetic platform just awaits a
    zero-sleep to yield control.
    """

    @abstractmethod
    async def search(
        self, query: str, user_id: str, limit: int = 10
    ) -> list[RankedItem]:
        """Return ranked search results for ``query``."""

    @abstractmethod
    async def recommend(
        self, user_id: str, n: int = 12, context: dict | None = None
    ) -> list[RankedItem]:
        """Return the ``n`` recommended tiles for ``user_id``, best first."""

    @abstractmethod
    async def record_event(self, event: dict) -> None:
        """Persist one interaction event."""

    def sample_intent_item(
        self, taste: np.ndarray, rng: np.random.Generator
    ) -> "ContentItem | None":
        """Sample a catalog item the persona "has in mind", or None.

        The session engine uses this to generate title-based search queries
        ("crimson prot..."). Adapters backed by a real catalog they can
        sample from may override this; the default ``None`` tells the
        engine to fall back to genre-term queries drawn from the persona's
        taste vector, so arbitrary adapters work with no extra code.
        """
        return None


class SyntheticPlatform(PlatformAdapter):
    """In-process reference platform: catalog + baseline recommender.

    The baseline scores ``popularity_weight * popularity +
    (1 - popularity_weight) * content_similarity`` where similarity is
    measured against the centroid of the user's *observed* history
    (clicked/watched items). Cold users get mostly popularity (weight
    0.8); once behavior exists the weight drops to ``popularity_weight``
    (default 0.25) so taste-correlated signal dominates — otherwise the
    trained model would learn popularity, not personalization.
    A small deterministic jitter breaks score ties without changing order
    semantics.
    """

    def __init__(
        self,
        catalog: SyntheticCatalog,
        seed: int = 11,
        popularity_weight: float = 0.25,
    ) -> None:
        self.catalog = catalog
        self.popularity_weight = popularity_weight
        self._rng = np.random.default_rng(seed)
        self._history: dict[str, list[str]] = {}  # user_id -> item_ids
        self.recorded: list[dict] = []  # every event passed to record_event

    # -- history -------------------------------------------------------------
    def note_interaction(self, user_id: str, item_id: str) -> None:
        self._history.setdefault(user_id, []).append(item_id)

    def history_vector(self, user_id: str) -> np.ndarray | None:
        ids = self._history.get(user_id)
        if not ids:
            return None
        vecs = [self.catalog.by_id[i].genre_vector for i in ids if i in self.catalog.by_id]
        if not vecs:
            return None
        centroid = np.mean(vecs, axis=0)
        norm = float(np.linalg.norm(centroid))
        return centroid / norm if norm > 0 else None

    # -- adapter ---------------------------------------------------------------
    async def search(
        self, query: str, user_id: str, limit: int = 10
    ) -> list[RankedItem]:
        await _yield()
        return self.catalog.search(query, limit=limit)

    async def recommend(
        self, user_id: str, n: int = 12, context: dict | None = None
    ) -> list[RankedItem]:
        await _yield()
        hist = self.history_vector(user_id)
        # Cold users get popularity; once behavior exists, content
        # similarity takes over. This is the honest shape of a baseline:
        # popularity is all you have until the user does something.
        pop_w = 0.8 if hist is None else self.popularity_weight
        scored: list[tuple[float, ContentItem]] = []
        for item in self.catalog.items:
            sim = 0.0 if hist is None else float(np.dot(hist, item.genre_vector))
            jitter = float(self._rng.uniform(0, 1e-6))
            score = pop_w * item.popularity + (1.0 - pop_w) * sim + jitter
            scored.append((score, item))
        scored.sort(key=lambda s: s[0], reverse=True)
        return [
            RankedItem(item=item, score=score, rank=r)
            for r, (score, item) in enumerate(scored[:n])
        ]

    async def record_event(self, event: dict) -> None:
        await _yield()
        self.recorded.append(event)
        etype = event.get("type")
        if etype in ("click", "play"):
            self.note_interaction(event["persona_id"], event["item_id"])

    # -- sampling helpers for the session engine --------------------------------
    def sample_intent_item(
        self, taste: np.ndarray, rng: np.random.Generator
    ) -> ContentItem:
        """Sample a catalog item proportional to taste·genre_vector.

        This models "the user has something in mind" — the session engine
        knows the persona's taste (it's the simulator), the platform does not.
        """
        weights = np.array(
            [max(float(np.dot(taste, it.genre_vector)), 1e-9) for it in self.catalog.items]
        )
        weights = weights / weights.sum()
        idx = int(rng.choice(len(self.catalog.items), p=weights))
        return self.catalog.items[idx]


async def _yield() -> None:
    """Yield control to the event loop without adding latency."""
    import asyncio

    await asyncio.sleep(0)


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    return float(np.dot(a, b) / denom) if denom > 0 else 0.0
