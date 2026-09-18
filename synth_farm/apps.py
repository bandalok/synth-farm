"""The apps layer: which streaming apps synthetic viewers use.

On a CTV platform the viewer doesn't live inside one app — they launch
apps from the home screen. This module models that:

* ``APPS`` — the platform's app catalog, with a popularity weight per app.
* ``sample_subscriptions`` — which apps a persona pays for. Each app is
  included with probability scaled by its popularity; everyone ends up
  with at least one.
* ``sample_app_affinity`` — how much the persona likes each of their
  subscribed apps: a Dirichlet vector, so it sums to 1.
* ``choose_app`` — pick one app for a session, proportional to affinity.

Everything takes an explicit ``numpy.random.Generator`` so runs are
reproducible. Callers must pass the persona's own child RNG *after* all
pre-existing draws, so the new draws never disturb established seeds.
"""

from __future__ import annotations

from typing import Sequence

import numpy as np

#: Apps available on the platform, with relative popularity weights.
APPS: tuple[str, ...] = (
    "Netflix",
    "Disney+",
    "HBO Max",
    "Hulu",
    "Prime Video",
    "Apple TV+",
    "Peacock",
    "Paramount+",
)

APP_POPULARITY: tuple[float, ...] = (
    0.24,  # Netflix
    0.16,  # Disney+
    0.14,  # HBO Max
    0.13,  # Hulu
    0.13,  # Prime Video
    0.08,  # Apple TV+
    0.07,  # Peacock
    0.05,  # Paramount+
)

#: Dirichlet concentration for the affinity vector. >1 keeps every
#: subscribed app in the mix; the persona's favourite still dominates.
AFFINITY_ALPHA = 1.5


def sample_subscriptions(rng: np.random.Generator) -> tuple[str, ...]:
    """Which apps this persona subscribes to.

    Each app is included independently with probability
    ``popularity * 3``, clipped to [0, 0.95] — the average persona ends up
    with ~3 subscriptions. Guarantees at least one: if the draw comes up
    empty, the persona gets the most popular app.
    """
    picked = [
        app
        for app, pop in zip(APPS, APP_POPULARITY)
        if rng.random() < min(max(pop * 3.0, 0.0), 0.95)
    ]
    if not picked:
        picked = [APPS[int(np.argmax(APP_POPULARITY))]]
    return tuple(picked)


def sample_app_affinity(
    rng: np.random.Generator, subscribed: Sequence[str]
) -> np.ndarray:
    """Affinity over the subscribed apps: a Dirichlet vector summing to 1.

    Position *i* of the returned vector is the affinity for
    ``subscribed[i]``.
    """
    n = len(subscribed)
    if n == 0:
        raise ValueError("sample_app_affinity needs at least one subscribed app")
    alpha = np.full(n, AFFINITY_ALPHA)
    return rng.dirichlet(alpha)


def choose_app(rng: np.random.Generator, persona) -> str:
    """Pick the app for one session, proportional to the persona's affinity.

    Personas built before the apps layer (no subscriptions on record)
    fall back to a popularity-weighted pick across all apps.
    """
    subscribed = tuple(getattr(persona, "subscribed_apps", None) or ())
    if not subscribed:
        w = np.array(APP_POPULARITY)
        return APPS[int(rng.choice(len(APPS), p=w / w.sum()))]
    affinity = getattr(persona, "app_affinity", None)
    if affinity is None or len(affinity) != len(subscribed):
        w = np.ones(len(subscribed))
    else:
        w = np.asarray(affinity, dtype=float)
        w = np.clip(w, 0.0, None)
        if w.sum() <= 0:
            w = np.ones(len(subscribed))
    return subscribed[int(rng.choice(len(subscribed), p=w / w.sum()))]
