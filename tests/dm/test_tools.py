"""Tool dispatch tests (M3): every DM tool lands on the engine, validated.

The dice are seeded so every roll is pinned; rejected calls change nothing
(the engine is the only writer, C3), and the authoritative strings carry the
numbers the narration must reflect verbatim.
"""

from __future__ import annotations

from pathlib import Path

from rpg.dm.spine import adventure_init_payload, load_adventure
from rpg.dm.tools import DM_TOOLS, ToolEngine
from rpg.engine.dice import SeededSource
from rpg.engine.state import WorldState

ADVENTURE_PATH = (
    Path(__file__).resolve().parents[2] / "src" / "rpg" / "content" / "shadow_crown.json"
)
PC = "Kellan Voss"


def seeded_world(seed: int = 42) -> tuple[WorldState, ToolEngine]:
    adventure = load_adventure(ADVENTURE_PATH)
    world = WorldState()
    world.apply_event("world_init", adventure_init_payload(adventure), 0)
    world.apply_event("spine_set", {"spine": adventure.spine.to_dict()}, 0)
    return world, ToolEngine(world, SeededSource(seed))


def tool_names() -> set[str]:
    return {t["function"]["name"] for t in DM_TOOLS}


# --- the schema surface -----------------------------------------------------------


def test_the_tool_schemas_cover_the_roadmap_list_plus_award():
    assert tool_names() == {
        "roll_check",
        "spawn_npc",
        "move_to",
        "start_combat",
        "end_combat",
        "grant_item",
        "take_item",
        "quest_update",
        "npc_note",
        "note",
        "ask_clarification",
        "award",
    }


def test_unknown_tools_are_rejected_without_side_effects():
    world, engine = seeded_world()
    before = world.to_dict()
    outcome = engine.dispatch("summon_army", {})
    assert outcome.ok is False
    assert "REJECTED" in outcome.message
    assert world.to_dict() == before
    assert engine.events == []


# --- checks -------------------------------------------------------------------------


def test_roll_check_is_deterministic_and_carries_the_dice_frame():
    world, engine = seeded_world()
    outcome = engine.dispatch("roll_check", {"ability": "wisdom", "dc": 12})
    assert outcome.ok is True
    die = outcome.dice[0]
    assert die["label"] == "wisdom check vs DC 12"
    assert die["dc"] == 12
    assert isinstance(die["total"], int)
    # The authoritative line carries the exact numbers (step 5).
    line = outcome.authoritative[0]
    assert "wisdom" in line and "DC 12" in line and str(die["total"]) in line
    # A check commits no world event — it is the engine's private roll.
    assert engine.events == []


def test_roll_check_rejects_bad_inputs():
    world, engine = seeded_world()
    for args in (
        {"ability": "luck", "dc": 12},
        {"ability": "wisdom", "dc": 0},
        {"ability": "wisdom", "dc": 31},
        {"ability": "wisdom", "dc": True},
        {"ability": "wisdom"},
    ):
        outcome = engine.dispatch("roll_check", args)
        assert outcome.ok is False, args
    assert engine.events == []


# --- world tools --------------------------------------------------------------------


def test_move_to_commits_the_pc_move_and_rejects_unknown_locations():
    world, engine = seeded_world()
    outcome = engine.dispatch("move_to", {"location": "The tavern"})
    assert outcome.ok is True
    assert engine.events == [("pc_move", {"location": "The tavern"})]
    assert world.pc.location == "The tavern"
    rejected = engine.dispatch("move_to", {"location": "The moon"})
    assert rejected.ok is False


def test_take_item_moves_a_loose_item_to_the_pc():
    world, engine = seeded_world()
    outcome = engine.dispatch("take_item", {"name": "Candle of Vell"})
    assert outcome.ok is True
    assert engine.events == [("item_set_holder", {"name": "Candle of Vell", "holder": f"pc:{PC}"})]
    assert world.items["Candle of Vell"].holder == f"pc:{PC}"
    again = engine.dispatch("take_item", {"name": "Candle of Vell"})
    assert again.ok is False  # already yours


def test_take_item_rejects_an_item_with_an_npc():
    world, engine = seeded_world()
    outcome = engine.dispatch("take_item", {"name": "Maren's herb pouch"})
    assert outcome.ok is False  # it is with npc:Old Maren


def test_grant_item_adds_and_places_items():
    world, engine = seeded_world()
    outcome = engine.dispatch(
        "grant_item", {"name": "A spare torch", "location": "The tavern", "description": "lit"}
    )
    assert outcome.ok is True
    assert "item_add" in [kind for kind, _ in engine.events]
    assert world.items["A spare torch"].location == "The tavern"
    held = engine.dispatch("grant_item", {"name": "A second torch", "holder": "npc:Brenna"})
    assert held.ok is True
    assert world.items["A second torch"].holder == "npc:Brenna"
    with_stranger = engine.dispatch("grant_item", {"name": "X", "holder": "npc:Stranger"})
    assert with_stranger.ok is False


def test_spawn_npc_adds_and_rejects_duplicates():
    world, engine = seeded_world()
    outcome = engine.dispatch(
        "spawn_npc",
        {"name": "The Stranger", "location": "The village square", "agenda": "keeper-only want"},
    )
    assert outcome.ok is True
    assert world.npcs["The Stranger"].location == "The village square"
    assert world.npcs["The Stranger"].agenda == "keeper-only want"
    duplicate = engine.dispatch("spawn_npc", {"name": "The Stranger"})
    assert duplicate.ok is False


def test_note_records_a_fact_the_spine_ticks():
    world, engine = seeded_world()
    outcome = engine.dispatch(
        "note", {"subject": "Brenna", "predicate": "spoke_with", "text": "Brenna"}
    )
    assert outcome.ok is True
    kind, payload = engine.events[0]
    assert kind == "fact_add"
    assert payload["value"] == "Brenna"
    assert world.spine.progress_events(world) == [{"op": "tick_waypoint", "beat": 0, "waypoint": 0}]


def test_npc_note_requires_a_known_npc():
    world, engine = seeded_world()
    assert engine.dispatch("npc_note", {"name": "Nobody", "text": "saw you"}).ok is False
    assert engine.dispatch("npc_note", {"name": "Brenna", "text": "saw you"}).ok is True


def test_quest_update_moves_status_and_rejects_bad_ones():
    world, engine = seeded_world()
    added = engine.dispatch(
        "quest_update",
        {
            "name": "The harvest",
            "status": "active",
            "description": "save the fields",
            "reward": "200 gp",
        },
    )
    assert added.ok is True
    assert world.quests["The harvest"].status == "active"
    done = engine.dispatch("quest_update", {"name": "The harvest", "status": "done"})
    assert done.ok is True
    assert world.quests["The harvest"].status == "done"
    assert engine.dispatch("quest_update", {"name": "The harvest", "status": "haunted"}).ok is False


def test_award_moves_xp_and_gold_and_rejects_negatives():
    world, engine = seeded_world()
    outcome = engine.dispatch("award", {"xp": 50, "gold": -3})
    assert outcome.ok is True
    assert world.pc.sheet.xp == 50
    assert world.pc.sheet.gold == 9
    assert engine.dispatch("award", {"xp": -1}).ok is False
    assert engine.dispatch("award", {"xp": 0, "gold": 0}).ok is False


def test_ask_clarification_ends_the_turn_with_the_question():
    world, engine = seeded_world()
    outcome = engine.dispatch("ask_clarification", {"question": "Which door do you mean?"})
    assert outcome.ok is True
    assert outcome.ends_turn is True
    assert outcome.message == "Which door do you mean?"
    assert engine.dispatch("ask_clarification", {"question": ""}).ok is False


def test_begin_turn_resets_the_turns_records():
    world, engine = seeded_world()
    engine.dispatch("note", {"subject": "Brenna", "predicate": "spoke_with", "text": "Brenna"})
    assert engine.events
    engine.begin_turn()
    assert engine.events == []
    assert engine.authoritative == []


# --- the combat bridge ------------------------------------------------------------------


def test_start_combat_resolves_the_encounter_by_the_engine():
    adventure = load_adventure(ADVENTURE_PATH)
    world, engine = seeded_world()
    encounter = adventure.encounters[0]  # the undercroft ambush: Marrow and Ash
    outcome = engine.dispatch("start_combat", {"participants": encounter["participants"]})
    assert outcome.ok is True
    # The whole encounter is committed, attack by attack (M1 mutation order).
    kinds = [kind for kind, _ in engine.events]
    assert kinds[0] == "combat_begin"
    assert kinds.count("combat_attack") >= 5
    assert kinds.count("combat_turn") >= 5
    # The seeded roll is pinned: the initiative order and the ending are exact.
    assert world.combat is not None
    assert world.combat.order == ["Ash", PC, "Marrow"]
    assert "victory" in outcome.message
    # The authoritative lines carry every number verbatim.
    assert any(line.startswith("Initiative:") for line in outcome.authoritative)
    assert any("Ash misses" in line for line in outcome.authoritative)


def test_start_combat_rejects_while_a_combat_is_in_progress():
    adventure = load_adventure(ADVENTURE_PATH)
    world, engine = seeded_world()
    encounter = adventure.encounters[0]
    assert engine.dispatch("start_combat", {"participants": encounter["participants"]}).ok
    again = engine.dispatch("start_combat", {"participants": encounter["participants"]})
    assert again.ok is False
    assert "already in progress" in again.message


def test_end_combat_derives_the_outcome_from_the_committed_state():
    adventure = load_adventure(ADVENTURE_PATH)
    world, engine = seeded_world()
    encounter = adventure.encounters[0]
    assert engine.dispatch("start_combat", {"participants": encounter["participants"]}).ok
    # Seed 42: the ambush ends in a PC victory (both cultists down, PC at 9 HP).
    outcome = engine.dispatch("end_combat", {"outcome": "victory", "xp_award": 50})
    assert outcome.ok is True
    assert world.combat is None
    assert world.pc.sheet.xp == 50
    assert world.pc.sheet.hp == 9
    # A mismatched claim is rejected by the engine.
    world2, engine2 = seeded_world()
    assert engine2.dispatch("start_combat", {"participants": encounter["participants"]}).ok
    wrong = engine2.dispatch("end_combat", {"outcome": "defeat"})
    assert wrong.ok is False
    assert "derives" in wrong.message


def test_end_combat_requires_a_combat_and_valid_outcome():
    world, engine = seeded_world()
    assert engine.dispatch("end_combat", {"outcome": "victory"}).ok is False
    assert engine.dispatch("end_combat", {"outcome": "triumph"}).ok is False
    assert engine.dispatch("end_combat", {"outcome": "victory", "xp_award": -5}).ok is False
