"""Persona generation for the synthetic user farm.

A persona is a bundle of behavioral parameters sampled from one of six
archetypes. The key modeling choices:

* Taste vectors are Dirichlet samples over the content taxonomy. The
  concentration parameter controls the shape of the population: small
  alpha -> spiky "loyalist" tastes, large alpha -> flat "explorer" tastes.
  This is where behavioral diversity comes from, and diversity is what
  makes the downstream recommender learn anything at all.
* Activity is Poisson (sessions/week), propensities are Beta. Everything
  flows from a seeded ``numpy.random.Generator`` so runs are reproducible.

If you take one thing from this module: when you eventually have real
users, fit these distributions to them. The simulator's assumptions are
the ceiling on what the trained model can learn.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

import numpy as np


@dataclass(frozen=True)
class Archetype:
    """A named parameter regime for a slice of the synthetic population."""

    name: str
    weight: float  # share of the population
    dirichlet_alpha: float  # base concentration for the taste vector
    dirichlet_spike: float  # extra alpha added to ONE random genre (0 = none)
    sessions_lambda: float  # mean sessions per week (Poisson)
    search_beta: tuple[float, float]  # Beta(a, b) for search-vs-browse propensity
    click_beta: tuple[float, float]  # Beta(a, b) for clickiness
    completion_beta: tuple[float, float]  # Beta(a, b) for completion propensity
    mean_units: float  # mean watch-units per session
    blurb: str


#: The six archetypes. Weights sum to 1.0.
ARCHETYPES: tuple[Archetype, ...] = (
    Archetype(
        name="binge_watcher",
        weight=0.18,
        dirichlet_alpha=1.0,
        dirichlet_spike=0.0,
        sessions_lambda=8.0,
        search_beta=(2.0, 8.0),  # rarely searches, lives on recommendations
        click_beta=(6.0, 3.0),
        completion_beta=(8.0, 2.0),  # finishes what they start
        mean_units=3.0,
        blurb="Heavy viewer. Browses rails, commits to full watches.",
    ),
    Archetype(
        name="genre_loyalist",
        weight=0.22,
        dirichlet_alpha=0.25,  # spiky base taste ...
        dirichlet_spike=3.0,  # ... plus one beloved genre
        sessions_lambda=4.0,
        search_beta=(4.0, 4.0),
        click_beta=(5.0, 3.0),
        completion_beta=(7.0, 3.0),
        mean_units=2.2,
        blurb="One genre owns 60%+ of their attention. Predictable, valuable.",
    ),
    Archetype(
        name="explorer",
        weight=0.15,
        dirichlet_alpha=3.0,  # flat, omnivorous taste
        dirichlet_spike=0.0,
        sessions_lambda=3.0,
        search_beta=(7.0, 3.0),  # hunts for things via search
        click_beta=(4.0, 4.0),
        completion_beta=(3.0, 5.0),  # samples widely, finishes rarely
        mean_units=2.0,
        blurb="Searches constantly, tries everything, commits to little.",
    ),
    Archetype(
        name="casual",
        weight=0.25,
        dirichlet_alpha=1.5,
        dirichlet_spike=0.0,
        sessions_lambda=1.5,
        search_beta=(3.0, 7.0),
        click_beta=(3.0, 5.0),
        completion_beta=(5.0, 5.0),
        mean_units=1.3,
        blurb="The median user. Shows up weekly, watches one thing.",
    ),
    Archetype(
        name="critic",
        weight=0.08,
        dirichlet_alpha=0.8,
        dirichlet_spike=1.5,
        sessions_lambda=2.0,
        search_beta=(5.0, 5.0),
        click_beta=(2.0, 8.0),  # hard to impress: low click-through
        completion_beta=(2.0, 6.0),  # bails fast when bored
        mean_units=1.5,
        blurb="Picky. Low CTR, high abandon. Your hardest eval slice.",
    ),
    Archetype(
        name="channel_surfer",
        weight=0.12,
        dirichlet_alpha=2.0,
        dirichlet_spike=0.0,
        sessions_lambda=6.0,
        search_beta=(1.0, 9.0),  # almost never searches
        click_beta=(8.0, 2.0),  # clicks everything ...
        completion_beta=(2.0, 5.0),  # ... finishes nothing
        mean_units=2.5,
        blurb="Clicks every tile, watches 10 minutes, moves on. Noisy signal.",
    ),
)


@dataclass
class Persona:
    """One synthetic user: an id, an archetype, and sampled behavior params."""

    persona_id: str
    archetype: str
    taste: np.ndarray  # shape (n_genres,), sums to 1
    sessions_per_week: int
    search_propensity: float  # P(search path) vs P(browse path) per session
    clickiness: float  # global multiplier on click probability
    completion_propensity: float  # multiplier on watch-completion probability
    mean_units: float
    genres: tuple[str, ...] = field(repr=False)

    def top_genres(self, k: int = 3) -> list[tuple[str, float]]:
        """The persona's favourite genres, highest weight first."""
        idx = np.argsort(self.taste)[::-1][:k]
        return [(self.genres[i], float(self.taste[i])) for i in idx]

    def sample_units(self, rng: np.random.Generator, cap: int) -> int:
        """How many watch-units this session gets (1 + Poisson, capped)."""
        lam = max(self.mean_units - 1.0, 0.0)
        return int(min(1 + rng.poisson(lam), cap))

    def describe(self) -> str:
        top = ", ".join(f"{g} {w:.0%}" for g, w in self.top_genres(3))
        return (
            f"{self.persona_id} [{self.archetype}] "
            f"taste=({top}) sessions/wk={self.sessions_per_week} "
            f"search={self.search_propensity:.2f} click={self.clickiness:.2f} "
            f"complete={self.completion_propensity:.2f}"
        )


def _sample_persona(
    index: int, genres: Sequence[str], rng: np.random.Generator
) -> Persona:
    """Sample one persona. Deterministic given the rng state."""
    names = [a.name for a in ARCHETYPES]
    weights = np.array([a.weight for a in ARCHETYPES])
    weights = weights / weights.sum()
    arch = ARCHETYPES[int(rng.choice(len(ARCHETYPES), p=weights))]

    # Taste vector: Dirichlet, with an optional spike on one genre.
    alpha = np.full(len(genres), arch.dirichlet_alpha)
    if arch.dirichlet_spike > 0:
        spike_idx = int(rng.integers(len(genres)))
        alpha[spike_idx] += arch.dirichlet_spike
    taste = rng.dirichlet(alpha)

    def beta(ab: tuple[float, float]) -> float:
        return float(rng.beta(ab[0], ab[1]))

    return Persona(
        persona_id=f"persona-{index:05d}",
        archetype=arch.name,
        taste=taste,
        sessions_per_week=int(rng.poisson(arch.sessions_lambda)),
        search_propensity=beta(arch.search_beta),
        clickiness=beta(arch.click_beta),
        completion_propensity=beta(arch.completion_beta),
        mean_units=arch.mean_units,
        genres=tuple(genres),
    )


def generate_personas(
    n: int,
    genres: Sequence[str],
    seed: int = 42,
) -> list[Persona]:
    """Generate ``n`` personas deterministically from ``seed``.

    Each persona draws from its own child RNG (spawned from a SeedSequence),
    so generating persona *i* never depends on how many came before it —
    the population is stable under reordering and sharding.
    """
    master = np.random.SeedSequence(seed)
    children = master.spawn(n)
    return [
        _sample_persona(i, genres, np.random.default_rng(child))
        for i, child in enumerate(children)
    ]


def archetype_distribution(personas: Sequence[Persona]) -> dict[str, float]:
    """Observed archetype shares in a generated population."""
    counts: dict[str, int] = {}
    for p in personas:
        counts[p.archetype] = counts.get(p.archetype, 0) + 1
    total = len(personas)
    return {name: counts.get(name, 0) / total for name in [a.name for a in ARCHETYPES]}


def population_report(personas: Sequence[Persona]) -> str:
    """Human-readable summary of a generated population."""
    dist = archetype_distribution(personas)
    lines = ["Persona population:"]
    for arch in ARCHETYPES:
        lines.append(f"  {arch.name:15s} {dist[arch.name]:6.1%}  — {arch.blurb}")
    sessions = [p.sessions_per_week for p in personas]
    lines.append(
        f"  mean sessions/week: {float(np.mean(sessions)):.2f} "
        f"(median {float(np.median(sessions)):.0f})"
    )
    return "\n".join(lines)
