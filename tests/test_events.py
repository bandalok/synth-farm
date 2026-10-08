"""Tests for the event schema, validation, and sinks."""

import json
import os

import pytest

from synth_farm import events as ev


def _search_event():
    return ev.make_search("persona-00001", "sess-1", "horror", 8, "slate-abc")


def test_builders_produce_valid_events():
    e = _search_event()
    assert ev.validate_event(e) is e
    assert e["synthetic"] is True
    assert e["type"] == "search"
    assert e["slate_kind"] == "search"


def test_all_builders_validate():
    events = [
        ev.make_search("p", "s", "q", 3, "sl"),
        ev.make_impression("p", "s", "sl", "reco", 0, "item-1"),
        ev.make_click("p", "s", "sl", 2, "item-1"),
        ev.make_play("p", "s", "item-1", 90),
        ev.make_quartile("p", "s", "item-1", 50),
        ev.make_complete("p", "s", "item-1"),
        ev.make_abandon("p", "s", "item-1", 0.4, "bored"),
    ]
    for e in events:
        ev.validate_event(e)


def test_synthetic_flag_is_mandatory():
    e = _search_event()
    e["synthetic"] = False
    with pytest.raises(ev.EventValidationError):
        ev.validate_event(e)
    del e["synthetic"]
    with pytest.raises(ev.EventValidationError):
        ev.validate_event(e)


def test_missing_field_rejected():
    e = _search_event()
    del e["query"]
    with pytest.raises(ev.EventValidationError):
        ev.validate_event(e)


def test_unknown_type_rejected():
    e = _search_event()
    e["type"] = "purchase"
    with pytest.raises(ev.EventValidationError):
        ev.validate_event(e)


def test_abandon_reason_constrained():
    with pytest.raises(AssertionError):
        ev.make_abandon("p", "s", "i", 0.3, "meh")


def test_abandon_fraction_clamped():
    e = ev.make_abandon("p", "s", "i", 1.7, "bored")
    assert e["watch_fraction"] == 1.0


def test_jsonl_roundtrip(tmp_path):
    path = str(tmp_path / "events.jsonl")
    sink = ev.JSONLSink(path)
    made = [
        ev.make_search("p1", "s1", "comedy", 5, "sl1"),
        ev.make_click("p1", "s1", "sl1", 1, "item-9"),
        ev.make_complete("p1", "s1", "item-9"),
    ]
    for e in made:
        sink.write(e)
    sink.close()
    assert sink.count == 3
    assert sink.by_type == {"search": 1, "click": 1, "complete": 1}
    loaded = ev.load_events_jsonl(path)
    assert loaded == made


def test_jsonl_rejects_invalid_line(tmp_path):
    path = str(tmp_path / "bad.jsonl")
    with open(path, "w") as fh:
        fh.write('{"type": "click"}\n')
    with pytest.raises(ev.EventValidationError):
        ev.load_events_jsonl(path)


def test_sqlite_roundtrip(tmp_path):
    path = str(tmp_path / "events.db")
    sink = ev.SQLiteSink(path)
    sink.write(ev.make_play("p2", "s2", "item-3", 45))
    sink.write(ev.make_quartile("p2", "s2", "item-3", 25))
    sink.close()
    import sqlite3
    conn = sqlite3.connect(path)
    rows = conn.execute(
        "SELECT type, synthetic, persona_id, payload FROM events ORDER BY type"
    ).fetchall()
    conn.close()
    assert len(rows) == 2
    assert all(r[1] == 1 for r in rows)  # synthetic flag stored
    assert all(r[2] == "p2" for r in rows)
    payload = json.loads(rows[0][3])
    assert "item_id" in payload


def test_sqlite_sink_validates(tmp_path):
    sink = ev.SQLiteSink(str(tmp_path / "x.db"))
    with pytest.raises(ev.EventValidationError):
        sink.write({"type": "click"})  # missing everything
    sink.close()


def test_summarize_counts():
    made = [
        ev.make_search("p1", "s1", "q", 2, "sl"),
        ev.make_search("p1", "s2", "q", 2, "sl"),
        ev.make_click("p2", "s3", "sl", 0, "i"),
    ]
    s = ev.summarize(made)
    assert s["total"] == 3
    assert s["by_type"] == {"search": 2, "click": 1}
    assert s["personas"] == 2
    assert s["sessions"] == 3


def test_memory_sink_collects():
    sink = ev.MemorySink()
    sink.write(_search_event())
    assert len(sink.events) == 1
    sink.close()
