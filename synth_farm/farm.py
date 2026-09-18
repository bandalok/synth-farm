"""The farm runner: N synthetic agents as asyncio tasks.

Each persona becomes one agent task that works through its precomputed
session schedule over the simulated time window. Design notes:

* **Determinism with concurrency.** Every session draws from its own
  child RNG (spawned from a SeedSequence keyed by persona + session
  index), so a session's *events* are identical regardless of task
  interleaving. The *order* events land in the sink may vary run to run —
  compare sorted event streams, not raw order, in tests.
* **Rate limiting.** A global ``asyncio.Semaphore`` wraps every platform
  call via ``RateLimitedAdapter``. Against the in-process platform this
  is nearly free; against a real HTTP adapter it becomes backpressure.
* **Graceful shutdown.** ``CancelledError`` per agent stops cleanly after
  the current session; ``Farm.run`` cancels the rest on interrupt.
* **Speed.** This is a simulation, not real HTTP: 1000 agents over a
  simulated week against the in-process platform completes in well under
  a minute. Real adapters add their own latency/retries behind the same
  interface.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

import numpy as np

from . import events as ev
from .catalog import PlatformAdapter, RankedItem
from .config import Config
from .personas import Persona
from .session import SessionEngine, sample_session_times


class RateLimitedAdapter(PlatformAdapter):
    """Wraps any adapter with a global semaphore on platform calls."""

    def __init__(self, inner: PlatformAdapter, max_inflight: int) -> None:
        self._inner = inner
        self._sem = asyncio.Semaphore(max_inflight)

    @property
    def inner(self) -> PlatformAdapter:
        return self._inner

    async def search(
        self, query: str, user_id: str, limit: int = 10
    ) -> list[RankedItem]:
        async with self._sem:
            return await self._inner.search(query, user_id, limit)

    async def recommend(
        self, user_id: str, n: int = 12, context: dict | None = None
    ) -> list[RankedItem]:
        async with self._sem:
            return await self._inner.recommend(user_id, n, context)

    async def record_event(self, event: dict) -> None:
        async with self._sem:
            await self._inner.record_event(event)


@dataclass
class FarmResult:
    """Outcome of a farm run."""

    personas_run: int
    sessions_run: int
    events_emitted: int
    elapsed_s: float
    events_by_type: dict[str, int] = field(default_factory=dict)

    def report(self) -> str:
        eps = self.events_emitted / max(self.elapsed_s, 1e-9)
        lines = [
            "Farm run complete:",
            f"  personas : {self.personas_run}",
            f"  sessions : {self.sessions_run}",
            f"  events   : {self.events_emitted} ({eps:,.0f}/s)",
            f"  elapsed  : {self.elapsed_s:.1f}s",
            "  by type  :",
        ]
        for etype, count in sorted(self.events_by_type.items()):
            lines.append(f"    {etype:10s} {count:>8,}")
        return "\n".join(lines)


class Farm:
    """Runs the persona population as concurrent agents."""

    def __init__(
        self,
        config: Config,
        personas: list[Persona],
        platform: PlatformAdapter,
        sink: ev.EventSink,
        seed: int | None = None,
    ) -> None:
        self.config = config
        self.personas = personas
        self.sink = sink
        self.seed = config.seed if seed is None else seed
        limited = RateLimitedAdapter(platform, config.max_inflight_requests)
        self.engine = SessionEngine(config, limited, sink)
        self._stop = asyncio.Event()

    def stop(self) -> None:
        """Signal all agents to stop after their current session."""
        self._stop.set()

    async def run(self, days: int | None = None, progress: bool = True) -> FarmResult:
        days = self.config.sim_days if days is None else days
        t0 = time.perf_counter()

        # Precompute every agent's schedule from its own child RNG.
        master = np.random.SeedSequence(self.seed)
        agent_seeds = master.spawn(len(self.personas))
        schedules: list[list[tuple[int, float]]] = []
        for persona, child in zip(self.personas, agent_seeds):
            rng = np.random.default_rng(child)
            schedules.append(
                sample_session_times(persona, days, rng, self.config)
            )

        sessions_run = 0
        tasks = [
            asyncio.create_task(
                self._agent_loop(persona, sched, agent_seed, days)
            )
            for persona, sched, agent_seed in zip(
                self.personas, schedules, agent_seeds
            )
        ]
        try:
            counts = await asyncio.gather(*tasks)
            sessions_run = sum(counts)
        except asyncio.CancelledError:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        finally:
            self.sink.close()

        elapsed = time.perf_counter() - t0
        by_type: dict[str, int] = dict(
            getattr(self.sink, "by_type", {}) or {}
        )
        if not by_type and isinstance(self.sink, ev.MemorySink):
            for e in self.sink.events:
                by_type[e["type"]] = by_type.get(e["type"], 0) + 1
        if progress:
            print(f"[farm] {self.engine.emitted:,} events from "
                  f"{sessions_run:,} sessions in {elapsed:.1f}s")
        return FarmResult(
            personas_run=len(self.personas),
            sessions_run=sessions_run,
            events_emitted=self.engine.emitted,
            elapsed_s=elapsed,
            events_by_type=by_type,
        )

    async def _agent_loop(
        self,
        persona: Persona,
        schedule: list[tuple[int, float]],
        agent_seed: np.random.SeedSequence,
        days: int,
    ) -> int:
        """One agent: work through its session schedule. Returns # sessions."""
        rng = np.random.default_rng(agent_seed)
        session_seeds = agent_seed.spawn(max(len(schedule), 1))
        done = 0
        try:
            for idx, ((day, hour), sseed) in enumerate(
                zip(schedule, session_seeds)
            ):
                if self._stop.is_set():
                    break
                if self.config.agent_jitter_max_s > 0:
                    await asyncio.sleep(
                        rng.random() * self.config.agent_jitter_max_s
                    )
                session_id = f"sess-{persona.persona_id}-{day:02d}-{idx:03d}"
                srg = np.random.default_rng(sseed)
                await self.engine.run_session(
                    persona, session_id, day, hour, srg
                )
                done += 1
                n = self.engine.emitted
                if n and n % self.config.progress_every_n_events == 0:
                    print(f"[farm] {n:,} events emitted…")
        except asyncio.CancelledError:
            pass  # stop cleanly after the current session
        return done
