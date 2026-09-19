"""The session engine: turns a persona into a stream of realistic sessions.

A session is one sitting: the persona arrives (at a diurnally-plausible
hour), then performs 1..N "units". Each unit is either:

* **search path** — sample an intent from the persona's taste, render it as
  a query (title fragment, genre term, occasional typo), call the
  platform's search, scan the slate with the cascade click model, and
  possibly watch the clicked item; or
* **browse path** — pull a recommendation slate, scan it the same way.

The **cascade click model** is the load-bearing piece of realism::

    P(click | rank) = clickiness * appeal**temperature / (1 + rank)**decay

where ``appeal = 0.7 * relevance + 0.3 * popularity`` and
``relevance = persona_taste . item_genre_vector``. The popularity term is
social proof (people click the hit show); the position decay is modeled
explicitly because a recommender trained on data *without* it learns a
feedback loop: top-ranked items get more clicks, which the model reads as
quality, which keeps them top-ranked. If your real surface has measurable
position bias (it does), the simulator must too.

The watch path emits ``play`` + ``quartile`` events and ends in either
``complete`` or ``abandon`` (reason: ``"bored"`` vs ``"interrupted"``),
with completion probability conditioned on relevance — people finish
things they like.

Timestamps are simulated: every session is anchored at a (day, hour) and
events advance a virtual clock, so a 7-day run produces a week of
plausible UTC timestamps.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Sequence

import numpy as np

from . import events as ev
from .apps import choose_app
from .catalog import ContentItem, PlatformAdapter, RankedItem
from .config import Config
from .personas import Persona

#: Simulated week starts on a Monday so day 5/6 are the weekend.
SIM_EPOCH = datetime(2026, 9, 14, 0, 0, 0, tzinfo=timezone.utc)

EmitFn = Callable[[dict], Awaitable[None]]


class _Clock:
    """A virtual clock that advances as simulated actions take time."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def tick(self, rng: np.random.Generator, lo: float, hi: float) -> str:
        """Advance by uniform(lo, hi) seconds; return the new ISO timestamp."""
        self.now += timedelta(seconds=float(rng.uniform(lo, hi)))
        return self.now.isoformat()

    def tick_minutes(self, minutes: float) -> str:
        self.now += timedelta(minutes=minutes)
        return self.now.isoformat()


def _stamp(event: dict, ts: str) -> dict:
    """Override an event's timestamp with the virtual clock's."""
    event["ts"] = ts
    return event


def sample_session_times(
    persona: Persona,
    days: int,
    rng: np.random.Generator,
    config: Config,
) -> list[tuple[int, float]]:
    """Sample ``(day, hour)`` arrival times for one persona.

    Session count ~ Poisson(sessions_per_week * days / 7); days weighted
    toward the weekend; hours drawn from an evening/lunch/uniform mixture
    with a dead zone in the small hours.
    """
    n = int(rng.poisson(persona.sessions_per_week * days / 7.0))
    if n == 0:
        return []
    # Weekly arrival pattern tiled to cover multi-week windows.
    week = np.array(
        [1.0, 1.0, 1.0, 1.0, 1.0, config.weekend_boost, config.weekend_boost]
    )
    day_w = np.tile(week, (days // 7) + 1)[:days]
    day_w = day_w / day_w.sum()

    times = [(int(rng.choice(days, p=day_w)), _sample_hour(rng, config))
             for _ in range(n)]
    times.sort()
    return times


def _sample_hour(rng: np.random.Generator, config: Config) -> float:
    """One arrival hour from the diurnal mixture."""
    for _ in range(8):  # rejection retries for the night dead-zone
        u = rng.random()
        if u < 0.62:
            h = float(rng.normal(config.evening_peak_hour, config.evening_peak_width))
        elif u < 0.62 + 0.38 * config.lunch_peak_weight:
            h = float(rng.normal(config.lunch_peak_hour, 1.2))
        else:
            h = float(rng.uniform(0, 24))
        h = min(max(h, 0.0), 23.99)
        if 2.0 <= h <= 5.0 and rng.random() < (1.0 - config.night_floor):
            continue  # dead zone: almost nobody starts a session at 3am
        return h
    return 21.0


def click_probability(
    persona: Persona,
    item: ContentItem,
    rank: int,
    config: Config,
) -> float:
    """P(click) for one tile.

    ``clickiness * appeal**temperature * position_decay(rank)`` where
    ``appeal = 0.7 * relevance + 0.3 * popularity``. The popularity term is
    social proof: people click the hit show even outside their taste, and
    without it a popularity-ranked slate would show near-zero CTR.
    """
    relevance = float(np.dot(persona.taste, item.genre_vector))
    appeal = 0.7 * relevance + 0.3 * item.popularity
    decay = 1.0 / ((1 + rank) ** config.position_bias_decay)
    p = persona.clickiness * (appeal ** config.click_temperature) * decay
    return float(min(max(p, 0.0), 0.95))


def make_query(
    intent: ContentItem,
    primary_genre: str,
    rng: np.random.Generator,
    typo_probability: float,
) -> str:
    """Render an intent as a plausible search query.

    People search with fragments ("crimson prot"), genre words ("horror"),
    or the full title when they know exactly what they want. Typos happen.
    """
    words = intent.title.split()
    r = rng.random()
    if r < 0.45 and len(words) >= 2 and len(words[1]) > 3:
        # Title fragment: first word + chopped second word. (The length
        # guard matters for real titles like "Land of Women": a 2-letter
        # second word can't be chopped.)
        second = words[1]
        cut = max(3, int(rng.integers(3, len(second) + 1)))
        query = f"{words[0]} {second[:cut]}"
    elif r < 0.65:
        query = primary_genre  # genre term, e.g. "documentary"
    elif r < 0.85:
        query = words[0]  # single distinctive word
    else:
        query = intent.title  # exact title: the "I know what I want" case

    if rng.random() < typo_probability and len(query) >= 5:
        query = _introduce_typo(query, rng)
    return query


def _introduce_typo(query: str, rng: np.random.Generator) -> str:
    """One small typo: adjacent transposition or a dropped character."""
    words = query.split()
    if not words:
        return query
    wi = int(rng.integers(len(words)))
    w = list(words[wi])
    if len(w) >= 4:
        if rng.random() < 0.5:
            i = int(rng.integers(len(w) - 1))
            w[i], w[i + 1] = w[i + 1], w[i]
        else:
            del w[int(rng.integers(len(w)))]
        words[wi] = "".join(w)
    return " ".join(words)


@dataclass
class SessionEngine:
    """Runs sessions for personas against a platform, emitting events."""

    config: Config
    platform: PlatformAdapter
    sink: ev.EventSink
    emitted: int = field(default=0, init=False)

    async def emit(self, event: dict) -> None:
        await self.platform.record_event(event)
        self.sink.write(event)
        self.emitted += 1

    # -- public -------------------------------------------------------------
    async def run_session(
        self,
        persona: Persona,
        session_id: str,
        day: int,
        hour: float,
        rng: np.random.Generator,
    ) -> list[dict]:
        """Run one full session; return the events it produced."""
        clock = _Clock(SIM_EPOCH + timedelta(days=day, hours=hour))
        happened: list[dict] = []

        async def _emit(e: dict) -> None:
            happened.append(e)
            await self.emit(e)

        # The viewer launches an app from the home screen before anything
        # else happens in the session.
        app_name = choose_app(rng, persona)
        await _emit(_stamp(
            ev.make_app_launch(persona.persona_id, session_id, app_name),
            clock.tick(rng, 1, 3),
        ))

        units = persona.sample_units(rng, self.config.max_units_per_session)
        for _ in range(units):
            if rng.random() < persona.search_propensity:
                await self._search_unit(persona, session_id, clock, rng, _emit)
            else:
                await self._browse_unit(persona, session_id, clock, rng, _emit)
        return happened

    # -- units ------------------------------------------------------------------
    async def _search_unit(
        self,
        persona: Persona,
        session_id: str,
        clock: _Clock,
        rng: np.random.Generator,
        emit: EmitFn,
    ) -> None:
        platform = self._inner_platform()
        intent = platform.sample_intent_item(persona.taste, rng)
        if intent is not None:
            catalog = getattr(platform, "catalog", None)
            genre_of = getattr(catalog, "genre_of", None)
            if callable(genre_of):
                genre = genre_of(intent)
            else:
                genre = self.config.genres[int(np.argmax(intent.genre_vector))]
        # Up to two query attempts: people refine when the first try misses.
        for attempt in range(2):
            if intent is not None:
                query = make_query(intent, genre, rng, self.config.typo_probability)
            else:
                # Generic adapter: no catalog to sample titles from, so the
                # persona searches by genre mood instead.
                query = self._genre_query(persona, rng)
            results = await self.platform.search(
                query, persona.persona_id, limit=self.config.search_result_limit
            )
            slate_id = f"slate-{uuid.uuid4().hex[:12]}"
            await emit(_stamp(
                ev.make_search(
                    persona.persona_id, session_id, query, len(results), slate_id
                ),
                clock.tick(rng, 1, 4),
            ))
            for ranked in results:
                await emit(_stamp(
                    ev.make_impression(
                        persona.persona_id, session_id, slate_id, "search",
                        ranked.rank, ranked.item.item_id,
                    ),
                    clock.tick(rng, 0.2, 1.5),
                ))
            clicked = await self._cascade(
                persona, session_id, slate_id, results, clock, rng, emit
            )
            if clicked or attempt == 1 or rng.random() < 0.7:
                return
            # Otherwise: refine the query and try once more.

    async def _browse_unit(
        self,
        persona: Persona,
        session_id: str,
        clock: _Clock,
        rng: np.random.Generator,
        emit: EmitFn,
    ) -> None:
        slate = await self.platform.recommend(
            persona.persona_id, n=self.config.slate_size
        )
        slate_id = f"slate-{uuid.uuid4().hex[:12]}"
        clock.tick(rng, 2, 8)  # time spent looking at the rail
        for ranked in slate:
            await emit(_stamp(
                ev.make_impression(
                    persona.persona_id, session_id, slate_id, "reco",
                    ranked.rank, ranked.item.item_id,
                ),
                clock.tick(rng, 0.2, 1.0),
            ))
        await self._cascade(persona, session_id, slate_id, slate, clock, rng, emit)

    # -- cascade click model ------------------------------------------------------
    async def _cascade(
        self,
        persona: Persona,
        session_id: str,
        slate_id: str,
        slate: Sequence[RankedItem],
        clock: _Clock,
        rng: np.random.Generator,
        emit: EmitFn,
    ) -> bool:
        """Scan the slate top-down; click at most one tile. Returns clicked?"""
        for ranked in slate:
            p = click_probability(persona, ranked.item, ranked.rank, self.config)
            if rng.random() < p:
                await emit(_stamp(
                    ev.make_click(
                        persona.persona_id, session_id, slate_id,
                        ranked.rank, ranked.item.item_id,
                    ),
                    clock.tick(rng, 4, 45),  # dwell before the click
                ))
                await self._watch(persona, session_id, ranked.item, clock, rng, emit)
                return True
            # After each non-click the user may give up scanning.
            if rng.random() < 0.06 + 0.03 * ranked.rank:
                break
        return False

    # -- watch path -----------------------------------------------------------------
    async def _watch(
        self,
        persona: Persona,
        session_id: str,
        item: ContentItem,
        clock: _Clock,
        rng: np.random.Generator,
        emit: EmitFn,
    ) -> None:
        relevance = float(np.dot(persona.taste, item.genre_vector))
        p_complete = min(
            persona.completion_propensity * (0.25 + 0.75 * relevance), 0.98
        )
        await emit(_stamp(
            ev.make_play(
                persona.persona_id, session_id, item.item_id, item.duration_min
            ),
            clock.tick(rng, 1, 3),
        ))

        if rng.random() < p_complete:
            for q in (25, 50, 75):
                await emit(_stamp(
                    ev.make_quartile(
                        persona.persona_id, session_id, item.item_id, q
                    ),
                    clock.tick_minutes(item.duration_min * 0.25),
                ))
            await emit(_stamp(
                ev.make_complete(persona.persona_id, session_id, item.item_id),
                clock.tick_minutes(item.duration_min * 0.25),
            ))
            return

        # Abandon: watch fraction from a right-skewed beta — most bails are
        # early, a few are near the end.
        frac = float(0.05 + 0.85 * rng.beta(2.0, 5.0))
        reached = [q for q in (25, 50, 75) if frac * 100 >= q]
        for q in reached:
            await emit(_stamp(
                ev.make_quartile(persona.persona_id, session_id, item.item_id, q),
                clock.tick_minutes(item.duration_min * 0.25),
            ))
        leftover = max(frac - 0.25 * len(reached), 0.0)
        reason = "bored" if rng.random() < (1.0 - relevance) else "interrupted"
        await emit(_stamp(
            ev.make_abandon(
                persona.persona_id, session_id, item.item_id, frac, reason
            ),
            clock.tick_minutes(item.duration_min * leftover),
        ))

    # -- helpers ----------------------------------------------------------------------
    def _inner_platform(self) -> PlatformAdapter:
        """Unwrap the farm's rate limiter to reach the real platform."""
        platform = self.platform
        inner = getattr(platform, "inner", None)
        return inner if inner is not None else platform

    def _genre_query(self, persona: Persona, rng: np.random.Generator) -> str:
        """Fallback query for adapters without a samplable catalog.

        Draws a genre term directly from the persona's taste vector — the
        "I'm in the mood for horror" search — so generic PlatformAdapter
        implementations work without subclassing SessionEngine.
        """
        taste = persona.taste / max(persona.taste.sum(), 1e-9)
        gi = int(rng.choice(len(self.config.genres), p=taste))
        query = self.config.genres[gi]
        if rng.random() < self.config.typo_probability and len(query) >= 5:
            query = _introduce_typo(query, rng)
        return query
