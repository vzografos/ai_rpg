"""WorldState unit tests (M2): entity validation and the apply_event contract.

apply_event is the single writer — every clamp and rejection pinned here is
what keeps "the LLM never owns numbers" structural (the anti-hack layer).
"""

from __future__ import annotations

import pytest

from rpg.dm.spine import StorySpine
from rpg.engine.sheet import ABILITIES
from rpg.engine.state import (
    COMBAT_OUTCOMES,
    NPC_STATUSES,
    QUEST_STATUSES,
    CombatantRecord,
    CombatState,
    Fact,
    Item,
    Location,
    Npc,
    PlayerCharacter,
    Quest,
    WorldState,
)

# --- fixtures -----------------------------------------------------------------


def make_world() -> WorldState:
    """A fresh world: a PC in a square, one npc, one quest, one item, one fact."""
    world = WorldState()
    payload = {
        "pc": {
            "name": "Mira",
            "hp": 12,
            "max_hp": 12,
            "ac": 14,
            "abilities": {ability: 10 for ability in ABILITIES},
            "inventory": {"torch": 2},
            "gold": 10,
            "xp": 0,
        },
        "pc_location": "The village square",
        "locations": [
            {
                "name": "The village square",
                "description": "A quiet square.",
                "exits": ["The tavern"],
            },
            {"name": "The tavern", "description": "Woodsmoke and low voices.", "exits": []},
        ],
        "npcs": [
            {
                "name": "Brenna",
                "location": "The tavern",
                "description": "The tavern keeper, ink-stained fingers.",
                "agenda": "She is hunting the shadow crown and will not say so.",
            }
        ],
        "quests": [
            {
                "name": "Find the shadow crown",
                "description": "The mayor pays for it.",
                "reward": "200 gp",
            }
        ],
        "items": [
            {
                "name": "Rusty key",
                "holder": "",
                "location": "The village square",
                "description": "Smells of iron.",
            }
        ],
    }
    world.apply_event("world_init", payload, turn=0)
    world.apply_event(
        "fact_add", {"subject": "Brenna", "predicate": "fears", "value": "the crown"}, turn=1
    )
    return world


def make_combatant(name: str = "Grak", **overrides: object) -> dict:
    base = {
        "name": name,
        "ac": 12,
        "hp": 8,
        "max_hp": 8,
        "dexterity": 12,
        "attack_bonus": 3,
        "damage": "1d6+2",
        "conditions": [],
    }
    base.update(overrides)
    return base


def begin_combat(world: WorldState, participants: list[dict] | None = None) -> None:
    world.apply_event(
        "combat_begin",
        {
            "participants": participants
            or [
                make_combatant("Mira", hp=12, max_hp=12, dexterity=14, damage="1d8+1"),
                make_combatant("Grak"),
            ],
            "order": [{"name": "Mira", "initiative": 17}, {"name": "Grak", "initiative": 9}],
        },
        turn=0,
    )


# --- entity validation ----------------------------------------------------------


def test_npc_rejects_unknown_status():
    with pytest.raises(ValueError, match="status"):
        Npc(name="X", status="missing")


def test_npc_statuses_match_the_vocabulary():
    assert set(NPC_STATUSES) == {"alive", "dead"}


def test_quest_rejects_unknown_status():
    with pytest.raises(ValueError, match="status"):
        Quest(name="Q", status="abandoned")
    assert set(QUEST_STATUSES) == {"open", "active", "done", "skipped"}


def test_item_cannot_be_held_and_placed_at_once():
    with pytest.raises(ValueError, match="held and placed"):
        Item(name="K", holder="pc:Mira", location="The tavern")


def test_item_holder_must_be_prefixed():
    with pytest.raises(ValueError, match="holder"):
        Item(name="K", holder="Mira")
    assert Item(name="K", holder="").holder == ""


def test_combatant_record_validates_like_m1():
    with pytest.raises(ValueError, match="hp"):
        CombatantRecord(
            name="X", ac=10, hp=-1, max_hp=5, dexterity=10, attack_bonus=0, damage="1d6"
        )
    with pytest.raises(ValueError, match="condition"):
        CombatantRecord(**make_combatant(conditions=["charmed"]))


def test_entity_records_reject_empty_names_and_invalid_counts():
    with pytest.raises(ValueError, match="npc name"):
        Npc(name="")
    with pytest.raises(ValueError, match="item name"):
        Item(name="")
    with pytest.raises(ValueError, match="quantity"):
        Item(name="Torch", quantity=0)
    with pytest.raises(ValueError, match="quest name"):
        Quest(name="")
    with pytest.raises(ValueError, match="location name"):
        Location(name="")
    with pytest.raises(ValueError, match="fact turn"):
        Fact(subject="S", predicate="P", value="V", turn=-1)


def test_entity_records_reject_malformed_notes_and_combat_stats():
    sheet = make_world().pc.sheet
    with pytest.raises(ValueError, match="note entry must"):
        PlayerCharacter(sheet=sheet, notes=[{"turn": "one", "text": "bad"}])
    with pytest.raises(ValueError, match="note entry text"):
        Npc(name="Fen", notes=[{"turn": 1, "text": ""}])
    with pytest.raises(ValueError, match="combatant name"):
        CombatantRecord(**make_combatant(""))
    with pytest.raises(ValueError, match="max_hp"):
        CombatantRecord(**make_combatant(max_hp=0, hp=0))
    with pytest.raises(ValueError, match="ac"):
        CombatantRecord(**make_combatant(ac=0))
    with pytest.raises(ValueError, match="death_successes"):
        CombatantRecord(**make_combatant(death_successes=4))


def test_combat_state_rejects_invalid_turn_order_and_outcome():
    participants = {
        name: CombatantRecord(**make_combatant(name)) for name in ("Mira", "Grak")
    }
    with pytest.raises(ValueError, match="at least 2"):
        CombatState(participants=participants, order=["Mira"], index=0, round=0)
    with pytest.raises(ValueError, match="unique"):
        CombatState(participants=participants, order=["Mira", "Mira"], index=0, round=0)
    with pytest.raises(ValueError, match="exactly the participants"):
        CombatState(participants=participants, order=["Mira", "Stranger"], index=0, round=0)
    with pytest.raises(ValueError, match="index"):
        CombatState(participants=participants, order=["Mira", "Grak"], index=2, round=0)
    with pytest.raises(ValueError, match="round"):
        CombatState(participants=participants, order=["Mira", "Grak"], index=0, round=-1)
    with pytest.raises(ValueError, match="outcome"):
        CombatState(
            participants=participants,
            order=["Mira", "Grak"],
            index=0,
            round=0,
            outcome="draw",
        )


# --- world_init -------------------------------------------------------------------


def test_world_init_builds_every_entity():
    world = make_world()
    assert world.pc is not None and world.pc.sheet.name == "Mira"
    assert world.pc.location == "The village square"
    assert world.pc.sheet.hp == 12 and world.pc.sheet.gold == 10
    assert "torch" in world.pc.sheet.inventory
    assert set(world.npcs) == {"Brenna"}
    assert world.npcs["Brenna"].agenda  # keeper-only, stored
    assert set(world.quests) == {"Find the shadow crown"}
    assert set(world.items) == {"Rusty key"}
    assert set(world.locations) == {"The village square", "The tavern"}
    assert world.locations["The village square"].exits == ["The tavern"]
    assert len(world.facts) == 1 and world.facts[0].turn == 1


def test_spell_slot_events_are_validated_and_part_of_caster_state():
    world = WorldState()
    world.apply_event(
        "world_init",
        {"pc": {"name": "Mira", "spellcasting_class": "Wizard"}},
        turn=0,
    )
    sheet = world.pc.sheet
    assert sheet.spellcasting_class == "wizard"
    assert sheet.spell_slots == {1: 2}
    assert world.to_dict()["pc"]["max_spell_slots"] == {1: 2}
    legacy_pc = make_world().to_dict()["pc"]
    assert set(legacy_pc) == {
        "name", "hp", "max_hp", "ac", "abilities", "inventory", "gold", "xp",
        "location", "description", "notes",
    }

    world.apply_event("pc_spell_slot_spend", {"level": 1}, turn=1)
    assert sheet.spell_slots == {1: 1}
    world.apply_event("pc_spell_slot_spend", {"level": 1}, turn=1)
    assert sheet.spell_slots == {1: 0}
    with pytest.raises(ValueError, match="no level 1 spell slots remain"):
        world.apply_event("pc_spell_slot_spend", {"level": 1}, turn=1)
    with pytest.raises(ValueError, match="must be an int"):
        world.apply_event("pc_spell_slot_spend", {"level": True}, turn=1)
    with pytest.raises(ValueError, match="rest_type must be short|long"):
        world.apply_event("pc_rest", {"kind": "overnight"}, turn=1)

    world.apply_event("pc_rest", {"kind": "long"}, turn=2)
    assert sheet.spell_slots == {1: 2}


def test_world_init_requires_a_pc_and_rejects_non_empty_worlds():
    with pytest.raises(ValueError, match="pc"):
        WorldState().apply_event("world_init", {"locations": []}, turn=0)
    world = make_world()
    with pytest.raises(ValueError, match="non-empty"):
        world.apply_event("world_init", {"pc": {"name": "X"}}, turn=0)


def test_world_init_location_references_must_exist():
    with pytest.raises(ValueError, match="unknown location"):
        WorldState().apply_event(
            "world_init",
            {"pc": {"name": "Mira"}, "pc_location": "Nowhere", "locations": [{"name": "Square"}]},
            turn=0,
        )


def test_world_init_exit_references_must_exist():
    with pytest.raises(ValueError, match="unknown exit"):
        WorldState().apply_event(
            "world_init",
            {"pc": {"name": "Mira"}, "locations": [{"name": "A", "exits": ["B"]}]},
            turn=0,
        )


# --- pc clamping (the sheet cannot drift) -------------------------------------------


def test_pc_damage_clamps_at_zero():
    world = make_world()
    world.apply_event("pc_damage", {"amount": 5}, turn=1)
    assert world.pc.sheet.hp == 7
    world.apply_event("pc_damage", {"amount": 99}, turn=2)
    assert world.pc.sheet.hp == 0


def test_pc_damage_rejects_negative():
    world = make_world()
    with pytest.raises(ValueError, match="damage"):
        world.apply_event("pc_damage", {"amount": -1}, turn=1)


def test_pc_heal_clamps_at_max_hp():
    world = make_world()
    world.apply_event("pc_heal", {"amount": 99}, turn=1)
    assert world.pc.sheet.hp == 12  # clamped at max
    world.apply_event("pc_set_hp", {"hp": 3}, turn=2)
    assert world.pc.sheet.hp == 3
    with pytest.raises(ValueError, match="0..max_hp"):
        world.apply_event("pc_set_hp", {"hp": 13}, turn=3)


def test_pc_xp_derives_the_level():
    world = make_world()
    world.apply_event("pc_gain_xp", {"amount": 300}, turn=1)
    assert world.pc.sheet.xp == 300
    assert world.pc.sheet.level == 2  # derived, never stored
    with pytest.raises(ValueError, match="xp"):
        world.apply_event("pc_gain_xp", {"amount": -5}, turn=2)


def test_pc_gold_never_goes_negative():
    world = make_world()
    world.apply_event("pc_gold", {"amount": -4}, turn=1)
    assert world.pc.sheet.gold == 6
    with pytest.raises(ValueError, match="spend"):
        world.apply_event("pc_gold", {"amount": -99}, turn=2)


def test_pc_items_stack_and_validate_removal():
    world = make_world()
    world.apply_event("pc_item_add", {"name": "Ration", "quantity": 3}, turn=1)
    assert world.pc.sheet.inventory["Ration"] == 3
    world.apply_event("pc_item_remove", {"name": "Ration", "quantity": 2}, turn=2)
    assert world.pc.sheet.inventory["Ration"] == 1
    with pytest.raises(ValueError, match="cannot remove"):
        world.apply_event("pc_item_remove", {"name": "Ration", "quantity": 2}, turn=3)


def test_pc_move_requires_a_known_location():
    world = make_world()
    with pytest.raises(ValueError, match="unknown location"):
        world.apply_event("pc_move", {"location": "The void"}, turn=1)
    world.apply_event("pc_move", {"location": "The tavern"}, turn=1)
    assert world.pc.location == "The tavern"


def test_pc_notes_append_with_the_turn():
    world = make_world()
    world.apply_event("pc_note", {"text": "The gate was locked."}, turn=3)
    assert world.pc.notes == [{"turn": 3, "text": "The gate was locked."}]
    with pytest.raises(ValueError, match="non-empty"):
        world.apply_event("pc_note", {"text": ""}, turn=4)


def test_pc_events_require_a_pc():
    world = WorldState()
    with pytest.raises(ValueError, match="no player character"):
        world.apply_event("pc_damage", {"amount": 1}, turn=1)


def test_apply_event_rejects_unknown_kinds_and_bad_payload_types():
    world = make_world()
    with pytest.raises(ValueError, match="unknown event kind"):
        world.apply_event("teleport_everywhere", {}, turn=1)
    with pytest.raises(ValueError, match="must be a string"):
        world.apply_event("npc_add", {"name": 7}, turn=1)
    with pytest.raises(ValueError, match="must be an int"):
        world.apply_event("pc_damage", {"amount": True}, turn=1)
    with pytest.raises(ValueError, match="must be a list"):
        world.apply_event("location_add", {"name": "Annex", "exits": "The tavern"}, turn=1)
    begin_combat(world)
    with pytest.raises(ValueError, match="must be a bool"):
        world.apply_event(
            "combat_attack",
            {"attacker": "Mira", "defender": "Grak", "melee": "yes", "hit": False},
            turn=1,
        )


def test_entity_add_and_note_events_reject_empty_text():
    world = make_world()
    for kind, payload, message in (
        ("npc_add", {"name": ""}, "npc_add name"),
        ("npc_note", {"name": "Brenna", "text": ""}, "npc_note text"),
        ("item_add", {"name": ""}, "item_add name"),
        ("quest_add", {"name": ""}, "quest_add name"),
        ("location_add", {"name": ""}, "location_add name"),
    ):
        with pytest.raises(ValueError, match=message):
            world.apply_event(kind, payload, turn=1)


def test_world_lookup_helpers_report_unknown_entities():
    world = make_world()
    for lookup in (
        lambda: world.quest("missing"),
        lambda: world.location("missing"),
        lambda: world.item("missing"),
        lambda: world.combatant("Mira"),
    ):
        with pytest.raises(ValueError):
            lookup()


# --- npcs ------------------------------------------------------------------------


def test_npc_add_validates_and_rejects_duplicates():
    world = make_world()
    with pytest.raises(ValueError, match="already exists"):
        world.apply_event("npc_add", {"name": "Brenna"}, turn=1)
    world.apply_event("npc_add", {"name": "Old Fen", "location": "The village square"}, turn=1)
    assert world.npcs["Old Fen"].status == "alive"
    with pytest.raises(ValueError, match="unknown location"):
        world.apply_event("npc_add", {"name": "Ghost", "location": "Nowhere"}, turn=1)


def test_dead_npcs_stay_dead():
    world = make_world()
    world.apply_event("npc_set_status", {"name": "Brenna", "status": "dead"}, turn=1)
    assert world.npcs["Brenna"].status == "dead"
    with pytest.raises(ValueError, match="dead stay dead"):
        world.apply_event("npc_set_status", {"name": "Brenna", "status": "alive"}, turn=2)
    with pytest.raises(ValueError, match="status"):
        world.apply_event("npc_set_status", {"name": "Brenna", "status": "missing"}, turn=2)


def test_npc_note_and_location_require_a_known_npc():
    world = make_world()
    with pytest.raises(ValueError, match="unknown npc"):
        world.apply_event("npc_note", {"name": "Stranger", "text": "saw the crown"}, turn=1)
    world.apply_event("npc_note", {"name": "Brenna", "text": "Paid no attention."}, turn=1)
    assert world.npcs["Brenna"].notes == [{"turn": 1, "text": "Paid no attention."}]
    world.apply_event(
        "npc_set_location", {"name": "Brenna", "location": "The village square"}, turn=2
    )
    assert world.npcs["Brenna"].location == "The village square"


# --- items -----------------------------------------------------------------------


def test_item_add_and_holder_validation():
    world = make_world()
    world.apply_event("item_add", {"name": "Shadow crown", "holder": "npc:Brenna"}, turn=1)
    assert world.items["Shadow crown"].holder == "npc:Brenna"
    with pytest.raises(ValueError, match="unknown holder"):
        world.apply_event("item_add", {"name": "Crown 2", "holder": "npc:Stranger"}, turn=1)
    with pytest.raises(ValueError, match="unknown holder"):
        world.apply_event("item_add", {"name": "Crown 3", "holder": "pc:Someone"}, turn=1)
    with pytest.raises(ValueError, match="already exists"):
        world.apply_event("item_add", {"name": "Rusty key"}, turn=1)


def test_item_set_holder_moves_and_clears():
    world = make_world()
    world.apply_event("item_set_holder", {"name": "Rusty key", "holder": "pc:Mira"}, turn=1)
    assert world.items["Rusty key"].holder == "pc:Mira"
    assert world.items["Rusty key"].location == ""  # a held item is no longer placed
    world.apply_event("item_set_holder", {"name": "Rusty key", "holder": "npc:Brenna"}, turn=2)
    assert world.items["Rusty key"].holder == "npc:Brenna"
    with pytest.raises(ValueError, match="unknown item"):
        world.apply_event("item_set_holder", {"name": "Nope", "holder": "pc:Mira"}, turn=3)


# --- quests ------------------------------------------------------------------------


def test_quest_status_moves_one_way_only():
    world = make_world()
    quest = world.quests["Find the shadow crown"]
    world.apply_event("quest_set_status", {"name": quest.name, "status": "active"}, turn=1)
    assert world.quests[quest.name].status == "active"
    world.apply_event("quest_set_status", {"name": quest.name, "status": "done"}, turn=2)
    assert world.quests[quest.name].status == "done"
    with pytest.raises(ValueError, match="final"):
        world.apply_event("quest_set_status", {"name": quest.name, "status": "open"}, turn=3)
    with pytest.raises(ValueError, match="status"):
        world.apply_event("quest_set_status", {"name": quest.name, "status": "abandoned"}, turn=3)


def test_quest_add_rejects_duplicates_and_unknown_status():
    world = make_world()
    with pytest.raises(ValueError, match="already exists"):
        world.apply_event("quest_add", {"name": "Find the shadow crown"}, turn=1)
    with pytest.raises(ValueError, match="status"):
        world.apply_event("quest_add", {"name": "New", "status": "abandoned"}, turn=1)


# --- locations -----------------------------------------------------------------------


def test_location_add_rejects_duplicates():
    world = make_world()
    with pytest.raises(ValueError, match="already exists"):
        world.apply_event("location_add", {"name": "The tavern"}, turn=1)
    world.apply_event("location_add", {"name": "The crypt", "exits": ["The tavern"]}, turn=1)
    assert world.locations["The crypt"].exits == ["The tavern"]


# --- facts ------------------------------------------------------------------------------


def test_fact_add_and_remove_by_content_not_turn():
    world = make_world()
    fact = world.facts[0]
    world.apply_event(
        "fact_add",
        {"subject": fact.subject, "predicate": fact.predicate, "value": "something else"},
        turn=2,
    )
    assert len(world.facts) == 2
    # Removing at a LATER turn still matches by (subject, predicate, value).
    world.apply_event(
        "fact_remove",
        {"subject": fact.subject, "predicate": fact.predicate, "value": fact.value},
        turn=5,
    )
    assert len(world.facts) == 1
    assert world.facts[0].value == "something else"
    with pytest.raises(ValueError, match="fact not found"):
        world.apply_event("fact_remove", {"subject": "X", "predicate": "Y", "value": "Z"}, turn=6)
    with pytest.raises(ValueError, match="non-empty"):
        world.apply_event("fact_add", {"subject": "", "predicate": "Y", "value": "Z"}, turn=6)


def test_spine_and_summary_event_guards():
    world = WorldState()
    with pytest.raises(ValueError, match="spine object"):
        world.apply_event("spine_set", {"spine": None}, turn=0)
    with pytest.raises(ValueError, match="no spine"):
        world.apply_event("spine_delta", {"op": "append_detail"}, turn=0)

    story = StorySpine.from_dict(
        {
            "premise": "A crown must not be recrowned.",
            "stakes": "The village starves.",
            "antagonist": "Vesper",
            "beats": [{"text": "Find the crown", "act": 1}],
            "finale": "The fields recover.",
        }
    )
    world.spine = story
    with pytest.raises(ValueError, match="already set"):
        world.apply_event("spine_set", {"spine": story.to_dict()}, turn=0)
    with pytest.raises(ValueError, match="requires an op"):
        world.apply_event("spine_delta", {"op": ""}, turn=0)
    with pytest.raises(ValueError, match="non-empty summary"):
        world.apply_event("summary_set", {"summary": "", "watermark": 0}, turn=0)


# --- combat (committed results, never re-rolled) -------------------------------------------


def test_combat_begin_requires_a_fresh_combat_and_two_participants():
    world = make_world()
    with pytest.raises(ValueError, match="at least 2"):
        world.apply_event(
            "combat_begin",
            {
                "participants": [make_combatant("Solo")],
                "order": [{"name": "Solo", "initiative": 1}],
            },
            turn=0,
        )
    begin_combat(world)
    assert world.combat is not None
    assert world.combat.current == "Mira"  # first living in the committed order
    assert world.combat.round == 0
    with pytest.raises(ValueError, match="already in progress"):
        begin_combat(world)


def test_combat_begin_validates_the_order():
    world = make_world()
    payload = {
        "participants": [make_combatant("Mira"), make_combatant("Grak")],
        "order": [{"name": "Mira", "initiative": 5}, {"name": "Stranger", "initiative": 6}],
    }
    with pytest.raises(ValueError, match="stranger"):
        world.apply_event("combat_begin", payload, turn=0)
    payload["order"] = [{"name": "Mira", "initiative": 5}, {"name": "Mira", "initiative": 6}]
    with pytest.raises(ValueError, match="twice"):
        world.apply_event("combat_begin", payload, turn=0)
    payload["order"] = [{"name": "Mira", "initiative": 5}]
    with pytest.raises(ValueError, match="exactly the participants"):
        world.apply_event("combat_begin", payload, turn=0)


def test_combat_begin_rejects_duplicate_dead_participants_and_bad_order_entries():
    world = make_world()
    duplicate = {
        "participants": [make_combatant("Mira"), make_combatant("Mira")],
        "order": [{"name": "Mira", "initiative": 5}, {"name": "Mira", "initiative": 4}],
    }
    with pytest.raises(ValueError, match="duplicate combatant"):
        world.apply_event("combat_begin", duplicate, turn=0)

    no_living = {
        "participants": [
            make_combatant("Mira", hp=0),
            make_combatant("Grak", hp=0),
        ],
        "order": [{"name": "Mira", "initiative": 5}, {"name": "Grak", "initiative": 4}],
    }
    with pytest.raises(ValueError, match="no living combatant"):
        world.apply_event("combat_begin", no_living, turn=0)

    malformed_order = {
        "participants": [make_combatant("Mira"), make_combatant("Grak")],
        "order": [{"name": "Mira", "initiative": 5}, "Grak"],
    }
    with pytest.raises(ValueError, match="entry must be an object"):
        world.apply_event("combat_begin", malformed_order, turn=0)


def test_combat_attack_rejects_an_attacker_who_cannot_act():
    world = make_world()
    begin_combat(world)
    world.combat.participants["Grak"].hp = 0
    with pytest.raises(ValueError, match="attacker cannot act"):
        world.apply_event(
            "combat_attack",
            {"attacker": "Grak", "defender": "Mira", "melee": True, "hit": True},
            turn=1,
        )


def test_combat_turn_checks_actor_and_skips_dead_combatants_across_rounds():
    world = make_world()
    begin_combat(world)
    with pytest.raises(ValueError, match="not .* turn"):
        world.apply_event("combat_turn", {"actor": "Grak"}, turn=1)

    participants = [
        make_combatant("Mira"),
        make_combatant("Grak", hp=0),
        make_combatant("Shade", hp=0),
    ]
    world = make_world()
    world.apply_event(
        "combat_begin",
        {
            "participants": participants,
            "order": [
                {"name": "Mira", "initiative": 17},
                {"name": "Grak", "initiative": 9},
                {"name": "Shade", "initiative": 5},
            ],
        },
        turn=0,
    )
    world.apply_event("combat_turn", {"actor": "Mira"}, turn=1)
    assert world.combat.current == "Mira"
    assert world.combat.round == 1

    for record in world.combat.participants.values():
        record.hp = 0
    with pytest.raises(ValueError, match="no living combatant"):
        world.apply_event("combat_turn", {"actor": "Mira"}, turn=2)


def test_combat_attack_miss_changes_nothing():
    world = make_world()
    begin_combat(world)
    before = world.fingerprint()
    world.apply_event(
        "combat_attack",
        {"attacker": "Mira", "defender": "Grak", "melee": True, "hit": False, "damage": 0},
        turn=1,
    )
    assert world.fingerprint() == before
    assert world.combat.participants["Grak"].hp == 8


def test_combat_attack_hit_applies_the_committed_outcome():
    world = make_world()
    begin_combat(world)
    world.apply_event(
        "combat_attack",
        {
            "attacker": "Mira",
            "defender": "Grak",
            "melee": True,
            "hit": True,
            "damage": 7,
            "defender_hp": 1,
            "death_successes": 0,
            "death_failures": 0,
        },
        turn=1,
    )
    grak = world.combat.participants["Grak"]
    assert grak.hp == 1 and grak.death_failures == 0
    # Death save committed at 0 HP.
    world.apply_event(
        "combat_attack",
        {
            "attacker": "Mira",
            "defender": "Grak",
            "melee": True,
            "hit": True,
            "damage": 3,
            "defender_hp": 0,
            "death_successes": 0,
            "death_failures": 1,
        },
        turn=2,
    )
    grak = world.combat.participants["Grak"]
    assert grak.hp == 0 and grak.death_failures == 1
    # Two more failed death saves make the combatant dead...
    for failures in (2, 3):
        world.apply_event(
            "combat_attack",
            {
                "attacker": "Mira",
                "defender": "Grak",
                "melee": True,
                "hit": True,
                "damage": 1,
                "defender_hp": 0,
                "death_successes": 0,
                "death_failures": failures,
            },
            turn=2,
        )
    assert world.combat.participants["Grak"].is_dead
    # ...and a dead combatant cannot be attacked (5e, mirrored from M1).
    with pytest.raises(ValueError, match="dead"):
        world.apply_event(
            "combat_attack",
            {
                "attacker": "Mira",
                "defender": "Grak",
                "melee": True,
                "hit": True,
                "damage": 1,
                "defender_hp": 0,
                "death_successes": 0,
                "death_failures": 3,
            },
            turn=3,
        )


def test_combat_attack_payload_is_clamped():
    world = make_world()
    begin_combat(world)
    base = {"attacker": "Mira", "defender": "Grak", "melee": True, "hit": True, "damage": 1}
    with pytest.raises(ValueError, match="out of range"):
        world.apply_event(
            "combat_attack",
            {**base, "defender_hp": 99, "death_successes": 0, "death_failures": 0},
            turn=1,
        )
    with pytest.raises(ValueError, match="pending death saves"):
        world.apply_event(
            "combat_attack",
            {**base, "defender_hp": 5, "death_successes": 1, "death_failures": 0},
            turn=1,
        )
    with pytest.raises(ValueError, match="pending death saves"):
        world.apply_event(
            "combat_attack",
            {**base, "defender_hp": 1, "death_successes": 0, "death_failures": 1},
            turn=1,
        )
    with pytest.raises(ValueError, match="committed death save"):
        world.apply_event(
            "combat_attack",
            {**base, "defender_hp": 0, "death_successes": 0, "death_failures": 0},
            turn=1,
        )
    with pytest.raises(ValueError, match="0..3"):
        world.apply_event(
            "combat_attack",
            {**base, "defender_hp": 0, "death_successes": 4, "death_failures": 0},
            turn=1,
        )
    with pytest.raises(ValueError, match="stable and dead"):
        world.apply_event(
            "combat_attack",
            {**base, "defender_hp": 0, "death_successes": 3, "death_failures": 3},
            turn=1,
        )
    with pytest.raises(ValueError, match="not in this combat"):
        world.apply_event(
            "combat_attack",
            {
                **base,
                "attacker": "Stranger",
                "defender_hp": 1,
                "death_successes": 0,
                "death_failures": 0,
            },
            turn=1,
        )


def test_combat_attack_accepts_the_m1_reachable_outcomes():
    world = make_world()
    begin_combat(world)
    # Natural-20 death save: back to 1 HP with exactly one success kept (M1).
    world.apply_event(
        "combat_attack",
        {
            "attacker": "Mira",
            "defender": "Grak",
            "melee": True,
            "hit": True,
            "damage": 8,
            "defender_hp": 1,
            "death_successes": 1,
            "death_failures": 0,
        },
        turn=1,
    )
    assert world.combat.participants["Grak"].hp == 1
    # Stable with earlier failures (M1: successes 3, failures may linger).
    world.apply_event(
        "combat_attack",
        {
            "attacker": "Mira",
            "defender": "Grak",
            "melee": True,
            "hit": True,
            "damage": 2,
            "defender_hp": 0,
            "death_successes": 3,
            "death_failures": 2,
        },
        turn=2,
    )
    grak = world.combat.participants["Grak"]
    assert grak.hp == 0 and grak.death_successes == 3
    # A stable combatant is out of the fight: a further hit is a committed no-op (M1).
    world.apply_event(
        "combat_attack",
        {
            "attacker": "Mira",
            "defender": "Grak",
            "melee": True,
            "hit": True,
            "damage": 1,
            "defender_hp": 0,
            "death_successes": 3,
            "death_failures": 2,
        },
        turn=3,
    )
    assert grak.hp == 0 and grak.death_successes == 3 and grak.death_failures == 2


def test_combat_condition_adds_and_removes():
    world = make_world()
    begin_combat(world)
    world.apply_event(
        "combat_condition", {"target": "Grak", "condition": "poisoned", "add": True}, turn=1
    )
    assert "poisoned" in world.combat.participants["Grak"].conditions
    world.apply_event(
        "combat_condition", {"target": "Grak", "condition": "poisoned", "add": False}, turn=2
    )
    assert "poisoned" not in world.combat.participants["Grak"].conditions
    with pytest.raises(ValueError, match="condition"):
        world.apply_event(
            "combat_condition", {"target": "Grak", "condition": "charmed", "add": True}, turn=3
        )


def test_combat_heal_clamps_and_clears_death_saves():
    world = make_world()
    begin_combat(world)
    world.apply_event(
        "combat_attack",
        {
            "attacker": "Mira",
            "defender": "Grak",
            "melee": True,
            "hit": True,
            "damage": 8,
            "defender_hp": 0,
            "death_successes": 1,
            "death_failures": 1,
        },
        turn=1,
    )
    world.apply_event("combat_heal", {"target": "Grak", "amount": 99}, turn=2)
    grak = world.combat.participants["Grak"]
    assert grak.hp == 8 and grak.death_successes == 0 and grak.death_failures == 0
    with pytest.raises(ValueError, match="heal amount"):
        world.apply_event("combat_heal", {"target": "Grak", "amount": -1}, turn=3)


def test_combat_end_requires_an_outcome_and_clears():
    world = make_world()
    begin_combat(world)
    with pytest.raises(ValueError, match="outcome"):
        world.apply_event("combat_end", {"outcome": "draw"}, turn=1)
    assert set(COMBAT_OUTCOMES) == {"victory", "defeat", "fled"}
    world.apply_event("combat_end", {"outcome": "victory"}, turn=1)
    assert world.combat is None
    with pytest.raises(ValueError, match="no combat"):
        world.apply_event(
            "combat_attack",
            {
                "attacker": "Mira",
                "defender": "Grak",
                "melee": True,
                "hit": True,
                "damage": 1,
                "defender_hp": 1,
                "death_successes": 0,
                "death_failures": 0,
            },
            turn=2,
        )


def test_combat_events_require_a_combat():
    world = make_world()
    with pytest.raises(ValueError, match="no combat"):
        world.apply_event(
            "combat_condition", {"target": "X", "condition": "prone", "add": True}, turn=1
        )
    with pytest.raises(ValueError, match="no combat"):
        world.apply_event("combat_end", {"outcome": "victory"}, turn=1)


# --- fingerprint ---------------------------------------------------------------------------


def test_fingerprint_is_deterministic_and_sensitive():
    world = make_world()
    assert world.fingerprint() == make_world().fingerprint()
    world.apply_event("pc_damage", {"amount": 1}, turn=5)
    assert world.fingerprint() != make_world().fingerprint()
    assert len(world.fingerprint()) == 64  # sha256 hex
