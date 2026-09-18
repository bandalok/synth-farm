"""Interaction event schema and sinks.

Every event the farm emits follows this schema::

    {
      "event_id":   "<uuid4 hex>",
      "type":       "search" | "impression" | "click" | "play"
                    | "quartile" | "complete" | "abandon",
      "synthetic":  true,                 # ALWAYS true. Non-negotiable.
      "persona_id": "persona-00042",
      "session_id": "sess-...",
      "ts":         "2026-09-18T21:04:11.123456+00:00",  # UTC ISO-8601
      ...per-type fields...
    }

Per-type fields:

* ``search``     — ``query`` (str), ``num_results`` (int), ``slate_id``,
  ``slate_kind`` = ``"search"``
* ``impression`` — ``slate_id``, ``slate_kind`` (``"search"`` | ``"reco"``),
  ``rank`` (0-based), ``item_id``
* ``click``      — ``slate_id``, ``rank``, ``item_id``
* ``play``       — ``item_id``, ``duration_min``
* ``quartile``   — ``item_id``, ``quartile`` (25 | 50 | 75)
* ``complete``   — ``item_id``, ``watch_fraction`` (= 1.0)
* ``abandon``    — ``item_id``, ``watch_fraction`` (0..1),
  ``reason`` (``"bored"`` | ``"interrupted"``)

The ``synthetic`` flag exists so synthetic traffic can never be mistaken
for real user behavior downstream. ``validate_event`` enforces it, and the
sinks reject any event where it is missing or false.

Sinks: ``JSONLSink`` (one JSON object per line — the training loop reads
this) and ``SQLiteSink`` (single ``events`` table, payload as JSON).
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Iterable


EVENT_TYPES = (
    "search",
    "impression",
    "click",
    "play",
    "quartile",
    "complete",
    "abandon",
)

_REQUIRED_COMMON = ("event_id", "type", "synthetic", "persona_id", "session_id", "ts")

_PER_TYPE_REQUIRED: dict[str, tuple[str, ...]] = {
    "search": ("query", "num_results", "slate_id", "slate_kind"),
    "impression": ("slate_id", "slate_kind", "rank", "item_id"),
    "click": ("slate_id", "rank", "item_id"),
    "play": ("item_id", "duration_min"),
    "quartile": ("item_id", "quartile"),
    "complete": ("item_id", "watch_fraction"),
    "abandon": ("item_id", "watch_fraction", "reason"),
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _base(
    etype: str,
    persona_id: str,
    session_id: str,
    ts: str | None = None,
) -> dict[str, Any]:
    return {
        "event_id": uuid.uuid4().hex,
        "type": etype,
        "synthetic": True,
        "persona_id": persona_id,
        "session_id": session_id,
        "ts": ts or utc_now_iso(),
    }


# -- builders ---------------------------------------------------------------
def make_search(
    persona_id: str, session_id: str, query: str, num_results: int, slate_id: str
) -> dict[str, Any]:
    e = _base("search", persona_id, session_id)
    e.update(
        query=query, num_results=num_results, slate_id=slate_id, slate_kind="search"
    )
    return e


def make_impression(
    persona_id: str,
    session_id: str,
    slate_id: str,
    slate_kind: str,
    rank: int,
    item_id: str,
) -> dict[str, Any]:
    e = _base("impression", persona_id, session_id)
    e.update(
        slate_id=slate_id, slate_kind=slate_kind, rank=rank, item_id=item_id
    )
    return e


def make_click(
    persona_id: str, session_id: str, slate_id: str, rank: int, item_id: str
) -> dict[str, Any]:
    e = _base("click", persona_id, session_id)
    e.update(slate_id=slate_id, rank=rank, item_id=item_id)
    return e


def make_play(
    persona_id: str, session_id: str, item_id: str, duration_min: int
) -> dict[str, Any]:
    e = _base("play", persona_id, session_id)
    e.update(item_id=item_id, duration_min=duration_min)
    return e


def make_quartile(
    persona_id: str, session_id: str, item_id: str, quartile: int
) -> dict[str, Any]:
    assert quartile in (25, 50, 75)
    e = _base("quartile", persona_id, session_id)
    e.update(item_id=item_id, quartile=quartile)
    return e


def make_complete(
    persona_id: str, session_id: str, item_id: str
) -> dict[str, Any]:
    e = _base("complete", persona_id, session_id)
    e.update(item_id=item_id, watch_fraction=1.0)
    return e


def make_abandon(
    persona_id: str,
    session_id: str,
    item_id: str,
    watch_fraction: float,
    reason: str,
) -> dict[str, Any]:
    assert reason in ("bored", "interrupted")
    e = _base("abandon", persona_id, session_id)
    e.update(
        item_id=item_id,
        watch_fraction=max(0.0, min(1.0, watch_fraction)),
        reason=reason,
    )
    return e


# -- validation ---------------------------------------------------------------
class EventValidationError(ValueError):
    """Raised when an event violates the schema."""


def validate_event(event: dict[str, Any]) -> dict[str, Any]:
    """Validate an event dict against the schema; return it unchanged.

    Raises ``EventValidationError`` on any violation. The synthetic flag is
    checked first and strictly: it must be present and ``True``.
    """
    for key in _REQUIRED_COMMON:
        if key not in event:
            raise EventValidationError(f"missing required field: {key}")
    if event["synthetic"] is not True:
        raise EventValidationError("event['synthetic'] must be True")
    etype = event["type"]
    if etype not in EVENT_TYPES:
        raise EventValidationError(f"unknown event type: {etype!r}")
    for key in _PER_TYPE_REQUIRED[etype]:
        if key not in event:
            raise EventValidationError(f"{etype} event missing field: {key}")
    return event


# -- sinks ----------------------------------------------------------------------
class EventSink(ABC):
    """Where events go. Implementations must be safe to share across tasks."""

    @abstractmethod
    def write(self, event: dict[str, Any]) -> None:
        """Validate and persist one event."""

    @abstractmethod
    def close(self) -> None:
        """Flush and release resources."""

    def __enter__(self) -> "EventSink":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


class JSONLSink(EventSink):
    """Append-only JSON Lines sink. The training loop reads this format."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._fh = open(path, "a", encoding="utf-8")
        self.count = 0
        self.by_type: dict[str, int] = {}

    def write(self, event: dict[str, Any]) -> None:
        validate_event(event)
        self._fh.write(json.dumps(event, separators=(",", ":")) + "\n")
        self.count += 1
        self.by_type[event["type"]] = self.by_type.get(event["type"], 0) + 1

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()


class SQLiteSink(EventSink):
    """SQLite sink: one ``events`` table, per-type payload as JSON."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS events (
        event_id   TEXT PRIMARY KEY,
        type       TEXT NOT NULL,
        synthetic  INTEGER NOT NULL,
        persona_id TEXT NOT NULL,
        session_id TEXT NOT NULL,
        ts         TEXT NOT NULL,
        payload    TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_events_persona ON events(persona_id);
    CREATE INDEX IF NOT EXISTS idx_events_type ON events(type);
    """

    def __init__(self, path: str) -> None:
        self.path = path
        # check_same_thread=False: the farm shares one sink across coroutines
        # running on a single thread; sqlite handles it with serialized writes.
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.executescript(self.SCHEMA)
        self.count = 0

    def write(self, event: dict[str, Any]) -> None:
        validate_event(event)
        payload = {
            k: v
            for k, v in event.items()
            if k not in ("event_id", "type", "synthetic", "persona_id", "session_id", "ts")
        }
        self._conn.execute(
            "INSERT OR IGNORE INTO events "
            "(event_id, type, synthetic, persona_id, session_id, ts, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                event["event_id"],
                event["type"],
                1 if event["synthetic"] else 0,
                event["persona_id"],
                event["session_id"],
                event["ts"],
                json.dumps(payload, separators=(",", ":")),
            ),
        )
        self.count += 1

    def flush(self) -> None:
        self._conn.commit()

    def close(self) -> None:
        self._conn.commit()
        self._conn.close()


class MemorySink(EventSink):
    """In-memory sink for tests and the demo's quick path."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def write(self, event: dict[str, Any]) -> None:
        validate_event(event)
        self.events.append(event)

    def close(self) -> None:
        pass


def load_events_jsonl(path: str) -> list[dict[str, Any]]:
    """Read back a JSONL event file, validating each line."""
    events: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                events.append(validate_event(json.loads(line)))
            except (json.JSONDecodeError, EventValidationError) as exc:
                raise EventValidationError(f"{path}:{line_no}: {exc}") from exc
    return events


def summarize(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Quick counts per type + distinct personas/sessions."""
    counts: dict[str, int] = {}
    personas: set[str] = set()
    sessions: set[str] = set()
    for e in events:
        counts[e["type"]] = counts.get(e["type"], 0) + 1
        personas.add(e["persona_id"])
        sessions.add(e["session_id"])
    return {
        "total": sum(counts.values()),
        "by_type": counts,
        "personas": len(personas),
        "sessions": len(sessions),
    }
