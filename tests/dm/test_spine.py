"""Story spine unit tests (M3): schema validation, clamped deltas, the
server-ticked waypoint rules, and the player-safe projection.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from rpg.dm.spine import (
    Adventure,
    StorySpine,
    load_adventure,
)
from rpg.engine.state import WorldState

REPO_ROOT = Path(__file__).resolve().parents[2]
ADVENTURE_PATH = REPO_ROOT / "src" / "rpg" / "content" / "shadow_crown.json"


def spine() -> StorySpine:
    return StorySpine.from_dict(
        {
            "premise": "A crown must not be recrowned.",
            "stakes": "The village starves.",
            "antagonist": "Vesper the Hollow",
            "beats": [
                {
                    "text": "Beat one",
                    "act": 1,
                    "waypoints": [
                        {"kind": "npc", "text": "Brenna"},
                        {"kind": "place", "text": "The tavern"},
                    ],
                },
                {
                    "text": "Beat two",
                    "act": 1,
                    "waypoints": [{"kind": "item", "text": "Candle of Vell"}],
                },
                {
                    "text": "Beat three",
                    "act": 2,
                    "waypoints": [{"kind": "narrative", "text": "The end"}],
                },
            ],
            "cast": [{"name": "Brenna", "role": "keeper", "agenda": "Keeper of the candle."}],
            "events": [
                {
                    "kind": "reveal",
                    "name": "The legend",
                    "trigger": "bread is given",
                    "detail": "The crown was forged for the last king.",
                }
            ],
            "finale": "The light returns.",
        }
    )


def world_with_pc() -> WorldState:
    world = WorldState()
    world.apply_event(
        "world_init",
        {
            "pc": {"name": "Kellan", "hp": 10, "max_hp": 10, "ac": 12},
            "pc_location": "The village square",
            "locations": [
                {"name": "The village square", "exits": ["The tavern"]},
                {"name": "The tavern", "exits": []},
            ],
            "npcs": [{"name": "Brenna", "location": "The tavern"}],
            "items": [
                {
                    "name": "Candle of Vell",
                    "location": "The tavern",
                    "quantity": 1,
                    "description": "warm",
                }
            ],
        },
        0,
    )
    return world


# --- schema validation -----------------------------------------------------------


def test_from_dict_requires_the_core_fields():
    raw = spine().to_dict()
    for key in ("premise", "beats", "finale"):
        with pytest.raises(ValueError):
            broken = copy.deepcopy(raw)
            del broken[key]
            StorySpine.from_dict(broken)


def test_from_dict_rejects_unknown_waypoint_kinds():
    raw = spine().to_dict()
    raw["beats"][0]["waypoints"][0]["kind"] = "weather"
    with pytest.raises(ValueError, match="waypoint"):
        StorySpine.from_dict(raw)


def test_adventure_loads_the_pre_authored_content():
    adventure = load_adventure(ADVENTURE_PATH)
    assert adventure.title == "The Shadow Crown"
    assert adventure.spine.active_beat is not None
    assert adventure.pc_location == "The village square"
    assert len(adventure.encounters) == 3


def test_adventure_rejects_out_of_range_counts():
    adventure = load_adventure(ADVENTURE_PATH)
    raw = {
        "title": "Too small",
        "spine": adventure.spine.to_dict(),
        "pc": {"name": "X"},
        "pc_location": "Nowhere",
        "locations": adventure.locations[:2],  # needs 3-5
        "npcs": adventure.npcs,
        "encounters": adventure.encounters,
    }
    with pytest.raises(ValueError, match="locations"):
        Adventure.from_dict(raw)


# --- clamped deltas ---------------------------------------------------------------


def test_apply_delta_rejects_unknown_ops():
    with pytest.raises(ValueError, match="unknown spine op"):
        spine().apply_delta("rewrite_everything", {})


def test_a_completed_beat_can_never_be_rewritten():
    story = spine()
    story.apply_delta("beat_status", {"beat": 0, "status": "done"})
    with pytest.raises(ValueError, match="final"):
        story.apply_delta("beat_status", {"beat": 0, "status": "skipped"})
    with pytest.raises(ValueError, match="completed beat"):
        story.apply_delta("rewrite_beat", {"beat": 0, "text": "a different beat"})
    # Re-ticking a done waypoint is rejected once it is done.
    story2 = spine()
    story2.apply_delta("tick_waypoint", {"beat": 0, "waypoint": 0})
    with pytest.raises(ValueError, match="already done"):
        story2.apply_delta("tick_waypoint", {"beat": 0, "waypoint": 0})


def test_marking_done_activates_the_next_pending_beat():
    story = spine()
    assert story.active_beat.text == "Beat one"
    story.apply_delta("beat_status", {"beat": 0, "status": "done"})
    assert story.active_beat.text == "Beat two"
    story.apply_delta("beat_status", {"beat": 1, "status": "skipped"})
    assert story.active_beat.text == "Beat three"
    story.apply_delta("beat_status", {"beat": 2, "status": "done"})
    assert story.active_beat is None
    assert story.is_over


def test_append_detail_accrues_instead_of_overwriting():
    story = spine()
    story.apply_delta("append_detail", {"beat": 1, "text": "first note"})
    story.apply_delta("append_detail", {"beat": 1, "text": "second note"})
    assert story.beat(1).detail == "first note\nsecond note"


def test_rewriting_a_beat_is_current_act_only():
    story = spine()
    story.apply_delta("rewrite_beat", {"beat": 1, "text": "Beat two (revised)"})
    assert story.beat(1).text == "Beat two (revised)"
    with pytest.raises(ValueError, match="another act"):
        story.apply_delta("rewrite_beat", {"beat": 2, "text": "Beat three (no)"})


def test_deltas_replay_safely_from_the_serialized_form():
    story = spine()
    story.apply_delta("tick_waypoint", {"beat": 0, "waypoint": 0})
    story.apply_delta("append_detail", {"beat": 0, "text": "the tavern smells of smoke"})
    replay = StorySpine.from_dict(story.to_dict())
    assert replay.beat(0).waypoints[0].done is True
    assert replay.to_dict() == story.to_dict()


# --- the server-ticked waypoint rules ----------------------------------------------


def test_place_waypoint_ticks_when_the_pc_arrives():
    story = spine()
    world = world_with_pc()
    assert story.progress_events(world) == []
    world.apply_event("pc_move", {"location": "The tavern"}, 1)
    deltas = story.progress_events(world)
    assert deltas == [{"op": "tick_waypoint", "beat": 0, "waypoint": 1}]


def test_item_waypoint_ticks_only_when_the_pc_holds_it():
    story = spine()
    world = world_with_pc()
    # An item with an NPC holder or a bare location never ticks.
    world.apply_event("item_set_holder", {"name": "Candle of Vell", "holder": "npc:Brenna"}, 1)
    assert story.progress_events(world) == []
    world.apply_event("item_set_holder", {"name": "Candle of Vell", "holder": "pc:Kellan"}, 2)
    story.apply_delta("beat_status", {"beat": 0, "status": "done"})  # activate beat two
    assert story.progress_events(world) == [{"op": "tick_waypoint", "beat": 1, "waypoint": 0}]


def test_fact_waypoints_tick_from_recorded_facts():
    story = spine()
    world = world_with_pc()
    world.apply_event(
        "fact_add",
        {"subject": "Brenna", "predicate": "spoke_with", "value": "Brenna", "source": "dm"},
        1,
    )
    assert story.progress_events(world) == [{"op": "tick_waypoint", "beat": 0, "waypoint": 0}]


def test_only_the_active_beats_waypoints_are_considered():
    story = spine()
    world = world_with_pc()
    # Beat three's narrative waypoint is met, but beat three is not active yet.
    world.apply_event(
        "fact_add",
        {"subject": "X", "predicate": "noted", "value": "The end", "source": "dm"},
        1,
    )
    # Both of beat one's waypoints are met: a "Brenna" fact + the PC at the tavern.
    world.apply_event(
        "fact_add",
        {"subject": "Brenna", "predicate": "spoke_with", "value": "Brenna", "source": "dm"},
        2,
    )
    world.apply_event("pc_move", {"location": "The tavern"}, 3)
    # Only the active beat (one) ticks; beat three's met waypoint does not.
    assert story.progress_events(world) == [
        {"op": "tick_waypoint", "beat": 0, "waypoint": 0},
        {"op": "tick_waypoint", "beat": 0, "waypoint": 1},
    ]


def test_advance_events_only_on_a_fully_ticked_beat():
    story = spine()
    assert story.advance_events() == []
    story.apply_delta("tick_waypoint", {"beat": 0, "waypoint": 0})
    assert story.advance_events() == []
    story.apply_delta("tick_waypoint", {"beat": 0, "waypoint": 1})
    assert story.advance_events() == [{"op": "beat_status", "beat": 0, "status": "done"}]


def test_done_and_skipped_are_the_only_final_statuses():
    story = spine()
    for status in ("done", "skipped"):
        fresh = spine()
        fresh.apply_delta("beat_status", {"beat": 0, "status": status})
        assert fresh.beat(0).is_final
    with pytest.raises(ValueError, match="final"):
        story.apply_delta("beat_status", {"beat": 0, "status": "active"})


# --- the projections ---------------------------------------------------------------


def test_now_block_carries_the_keeper_only_view():
    block = spine().now_block()
    assert "Premise:" in block
    assert "NOW (beat 1 of 3, act 1): Beat one" in block
    assert "Brenna (keeper) — Keeper of the candle." in block  # agendas are DM-facing
    assert "Planned events" in block and "The legend" in block
    assert "Finale: The light returns." in block


def test_quest_lines_are_player_safe():
    story = spine()
    lines = story.quest_lines()
    assert lines[0].startswith("Current: Beat one")
    assert "  - Brenna" in lines and "  - The tavern" in lines
    assert any(line.startswith("Next: Beat two") for line in lines)
    # Keeper-only data never leaks into the player-facing projection.
    for line in lines:
        assert "Keeper of the candle" not in line
        assert "forged for the last king" not in line
