"""Tests for persona #8 — the Sports pivot (live_demo/server.py).

Mirrors the Bollywood-pivot tests: agent #8 (index 7) is a heavy sports
user from day 1, while every other regular persona keeps only a small
sports flavor and the hand-pinned sports/sci-fi anchors are untouched.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "live_demo"))
from server import LiveSim, ANCHOR_PINS  # noqa: E402


@pytest.fixture(scope="module")
def sim():
    return LiveSim(n_agents=60, seed=7, tick_seconds=0.05)


# ---------- Sports dimension ----------

def test_sports_is_dimension_25(sim):
    assert len(sim.genres) == 30
    assert sim.gidx["Sports"] == 25
    assert sim.genres[25] == "Sports"


# ---------- Persona #8: the Sports pivot ----------

def test_agent_eight_is_sports_pivot(sim):
    # "from day 1": assert on the fresh sim, before any taste drift.
    assert sim.sports_pivot_idx == 7
    assert sim.personas[7].persona_id.endswith("00007")
    sw = [float(sim.tastes[i][sim.gidx["Sports"]])
          for i in range(sim.base_agents)]
    assert sw[7] > 0.30, f"agent #8 Sports weight only {sw[7]:.3f}"
    assert sw.index(max(sw)) == 7, "agent #8 should be the heaviest sports user among regular agents"
    assert sim.genres[int(sim.tastes[7].argmax())] == "Sports"
    # The pivot lands on a generated persona, never on an appended anchor.
    assert sim.personas[7].persona_id not in sim.anchor_ids
    assert sim.personas[7].persona_id not in ANCHOR_PINS


def test_other_personas_keep_small_sports_flavor(sim):
    # Everyone else (regular population, Bollywood pivot included) starts
    # with only a small sports flavor — the dirichlet cold start.
    sw = [float(sim.tastes[i][sim.gidx["Sports"]])
          for i in range(sim.base_agents) if i not in (6, 7)]
    assert max(sw) < 0.15, f"a non-pivot agent starts {max(sw):.0%} sports"
    assert sim.genres[int(sim.tastes[0].argmax())] != "Sports"


def test_sports_pivot_home_rails_sports_leaning(sim):
    # Sports-leaning recommendations from day 1, like the anchors' rows.
    def sports_share(idx):
        hs = sim.home_screen(sim.personas[idx])
        total = sum(len(r["items"]) for r in hs["rails"])
        sp = sum(1 for r in hs["rails"] for x in r["items"]
                 if "Sports" in sim.by_id[x["id"]].genre_tags)
        return sp / total

    assert sports_share(7) > 0.30, "agent #8's rails should read sports-heavy"
    for idx in (0, 1, 2):
        assert sports_share(idx) < 0.10, f"agent #{idx + 1} sees too much sports"


# ---------- Anchors are untouched ----------

def test_sports_anchors_unchanged(sim):
    # The two hand-pinned sports anchors keep their exact tuned tastes and
    # their distinct behavior; persona #8 does not disturb them.
    duo = {p.persona_id: p for p in sim.personas
           if p.persona_id in ("persona-seamhead", "persona-socialfan")}
    assert len(duo) == 2
    a, b = duo["persona-seamhead"], duo["persona-socialfan"]
    assert a.archetype != b.archetype
    assert ANCHOR_PINS["persona-seamhead"] == "Sports"
    assert ANCHOR_PINS["persona-socialfan"] == "Sports"
    live_a = sim.tastes[sim.personas.index(a)]
    live_b = sim.tastes[sim.personas.index(b)]
    assert abs(float(live_a[sim.gidx["Sports"]]) - 0.521) < 0.01
    assert abs(float(live_b[sim.gidx["Sports"]]) - 0.366) < 0.01
    assert (a.sessions_per_week, a.search_propensity,
            a.completion_propensity) != (b.sessions_per_week,
                                         b.search_propensity,
                                         b.completion_propensity)


def test_scifi_anchors_unchanged(sim):
    pair = {p.persona_id: p for p in sim.personas
            if p.persona_id in ("persona-voidwalker", "persona-nebula")}
    assert len(pair) == 2
    a, b = pair["persona-voidwalker"], pair["persona-nebula"]
    assert a.archetype != b.archetype
    assert ANCHOR_PINS["persona-voidwalker"] == "Science Fiction"
    live_a = sim.tastes[sim.personas.index(a)]
    assert sim.genres[int(live_a.argmax())] == "Science Fiction"


# ---------- Bollywood pivot still intact ----------

def test_bollywood_pivot_still_intact(sim):
    # Persona #8 must not disturb the Bollywood pivot's assignment.
    assert sim.pivot_idx == 6
    assert sim.sports_pivot_idx != sim.pivot_idx
    bw = [float(sim.tastes[i][sim.gidx["Bollywood"]])
          for i in range(sim.n_agents)]
    assert bw.index(max(bw)) == 6
    assert sim.genres[int(sim.tastes[6].argmax())] == "Bollywood"
    assert sim.genres[int(sim.tastes[7].argmax())] == "Sports"


# ---------- Server payload flags ----------

def _api_agents_flags(sim, pi):
    """Replicates the /api/agents handler's flag fields exactly (they are
    inline in the route, no HTTP harness exists in this suite)."""
    return {"pivot": pi == sim.pivot_idx,
            "sports_pivot": pi == sim.sports_pivot_idx}


def test_payload_flags_mark_pivots(sim):
    assert _api_agents_flags(sim, 6) == {"pivot": True, "sports_pivot": False}
    assert _api_agents_flags(sim, 7) == {"pivot": False, "sports_pivot": True}
    assert _api_agents_flags(sim, 0) == {"pivot": False, "sports_pivot": False}
    # The Bollywood flag keeps its old meaning: only agent #7 gets it.
    assert [pi for pi in range(sim.n_agents)
            if _api_agents_flags(sim, pi)["pivot"]] == [6]


def test_agent_payload_marks_sports_pivot(sim):
    d7 = sim.agent_payload(sim.personas[7])
    assert d7["is_sports_pivot"] is True
    assert d7["is_pivot"] is False
    d6 = sim.agent_payload(sim.personas[6])
    assert d6["is_pivot"] is True
    assert d6["is_sports_pivot"] is False
    d0 = sim.agent_payload(sim.personas[0])
    assert d0["is_pivot"] is False and d0["is_sports_pivot"] is False
