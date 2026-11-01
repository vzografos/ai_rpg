"""Character sheet (roadmap M1): derived stats, XP/level table, mutations."""

from __future__ import annotations

import pytest

from rpg.engine.sheet import (
    ABILITIES,
    CharacterSheet,
    ability_modifier,
    level_for_xp,
    next_level_xp,
    proficiency_bonus,
    spell_slots_for,
    xp_progress,
)

# -- derived-stat tables ----------------------------------------------------


def test_ability_modifier_table() -> None:
    assert ability_modifier(3) == -4
    assert ability_modifier(8) == -1
    assert ability_modifier(9) == -1  # floor division
    assert ability_modifier(10) == 0
    assert ability_modifier(11) == 0
    assert ability_modifier(15) == 2
    assert ability_modifier(18) == 4
    assert ability_modifier(20) == 5
    assert ability_modifier(30) == 10


@pytest.mark.parametrize("score", [0, 2, 31, 100, -1])
def test_ability_modifier_rejects_out_of_range(score: int) -> None:
    with pytest.raises(ValueError, match="ability score out of range"):
        ability_modifier(score)


def test_proficiency_bonus_table() -> None:
    # SRD 5.1: levels 1-4: +1, 5-8: +2, 9-12: +3, 13-16: +4, 17-20: +5
    expected = {1: 1, 2: 1, 3: 1, 4: 1, 5: 2, 8: 2, 9: 3, 12: 3, 13: 4, 16: 4, 17: 5, 20: 5}
    for level, bonus in expected.items():
        assert proficiency_bonus(level) == bonus, level


@pytest.mark.parametrize("level", [0, 21, -3])
def test_proficiency_bonus_rejects_out_of_range(level: int) -> None:
    with pytest.raises(ValueError, match="level out of range"):
        proficiency_bonus(level)


def test_level_for_xp_boundaries() -> None:
    assert level_for_xp(0) == 1
    assert level_for_xp(299) == 1
    assert level_for_xp(300) == 2
    assert level_for_xp(899) == 2
    assert level_for_xp(900) == 3
    assert level_for_xp(2699) == 3
    assert level_for_xp(2700) == 4
    assert level_for_xp(3349999) == 19
    assert level_for_xp(3350000) == 20
    assert level_for_xp(99999999) == 20  # capped at 20


def test_level_for_xp_rejects_negative() -> None:
    with pytest.raises(ValueError, match="xp cannot be negative"):
        level_for_xp(-1)


def test_next_level_xp() -> None:
    assert next_level_xp(1) == 300
    assert next_level_xp(4) == 6500
    assert next_level_xp(20) is None
    with pytest.raises(ValueError, match="level out of range"):
        next_level_xp(21)
    with pytest.raises(ValueError, match="level out of range"):
        next_level_xp(0)


def test_spell_slot_progression_tables() -> None:
    full_caster = [
        {1: 2}, {1: 3}, {1: 4, 2: 2}, {1: 4, 2: 3}, {1: 4, 2: 3, 3: 2},
        {1: 4, 2: 3, 3: 3}, {1: 4, 2: 3, 3: 3, 4: 1},
        {1: 4, 2: 3, 3: 3, 4: 2}, {1: 4, 2: 3, 3: 3, 4: 3, 5: 1},
        {1: 4, 2: 3, 3: 3, 4: 3, 5: 2},
        {1: 4, 2: 3, 3: 3, 4: 3, 5: 2, 6: 1},
        {1: 4, 2: 3, 3: 3, 4: 3, 5: 2, 6: 1},
        {1: 4, 2: 3, 3: 3, 4: 3, 5: 2, 6: 1, 7: 1},
        {1: 4, 2: 3, 3: 3, 4: 3, 5: 2, 6: 1, 7: 1},
        {1: 4, 2: 3, 3: 3, 4: 3, 5: 2, 6: 1, 7: 1, 8: 1},
        {1: 4, 2: 3, 3: 3, 4: 3, 5: 2, 6: 1, 7: 1, 8: 1},
        {1: 4, 2: 3, 3: 3, 4: 3, 5: 2, 6: 1, 7: 1, 8: 1, 9: 1},
        {1: 4, 2: 3, 3: 3, 4: 3, 5: 3, 6: 1, 7: 1, 8: 1, 9: 1},
        {1: 4, 2: 3, 3: 3, 4: 3, 5: 3, 6: 2, 7: 1, 8: 1, 9: 1},
        {1: 4, 2: 3, 3: 3, 4: 3, 5: 3, 6: 2, 7: 2, 8: 1, 9: 1},
    ]
    for caster in ("bard", "cleric", "druid", "sorcerer", "wizard"):
        assert [spell_slots_for(caster, level) for level in range(1, 21)] == full_caster

    half_caster = [
        {}, {1: 2}, {1: 3}, {1: 3}, {1: 4, 2: 2}, {1: 4, 2: 2},
        {1: 4, 2: 3}, {1: 4, 2: 3}, {1: 4, 2: 3, 3: 2},
        {1: 4, 2: 3, 3: 2}, {1: 4, 2: 3, 3: 3}, {1: 4, 2: 3, 3: 3},
        {1: 4, 2: 3, 3: 3, 4: 1}, {1: 4, 2: 3, 3: 3, 4: 1},
        {1: 4, 2: 3, 3: 3, 4: 2}, {1: 4, 2: 3, 3: 3, 4: 2},
        {1: 4, 2: 3, 3: 3, 4: 3, 5: 1},
        {1: 4, 2: 3, 3: 3, 4: 3, 5: 1},
        {1: 4, 2: 3, 3: 3, 4: 3, 5: 2},
        {1: 4, 2: 3, 3: 3, 4: 3, 5: 2},
    ]
    for caster in ("paladin", "ranger"):
        assert [spell_slots_for(caster, level) for level in range(1, 21)] == half_caster

    warlock = [
        {1: 1}, {1: 2}, {2: 2}, {2: 2}, {3: 2}, {3: 2}, {4: 2}, {4: 2},
        {5: 2}, {5: 2}, {5: 3}, {5: 3}, {5: 3}, {5: 3}, {5: 3}, {5: 3},
        {5: 4}, {5: 4}, {5: 4}, {5: 4},
    ]
    assert [spell_slots_for("warlock", level) for level in range(1, 21)] == warlock
    assert spell_slots_for(None, 1) == {}
    assert spell_slots_for("Wizard", 1) == {1: 2}


@pytest.mark.parametrize("level", [True, 0, 21, "1"])
def test_spell_slot_progression_rejects_invalid_levels(level: object) -> None:
    with pytest.raises(ValueError, match="level out of range"):
        spell_slots_for("wizard", level)  # type: ignore[arg-type]


def test_spell_slot_progression_rejects_invalid_classes() -> None:
    with pytest.raises(ValueError, match="must be a string"):
        spell_slots_for(7, 1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unsupported spellcasting class"):
        spell_slots_for("fighter", 1)


def test_xp_progress_panel() -> None:
    sheet = CharacterSheet(name="Test", xp=0)
    assert xp_progress(sheet) == {
        "level": 1,
        "proficiency": 1,
        "xp": 0,
        "next_level_xp": 300,
        "xp_to_next": 300,
    }
    sheet.add_xp(300)  # level 2
    assert xp_progress(sheet)["level"] == 2
    assert xp_progress(sheet)["xp_to_next"] == 900 - 300
    sheet.xp = 3350000  # level 20: max level
    assert xp_progress(sheet)["next_level_xp"] is None
    assert xp_progress(sheet)["xp_to_next"] is None


# -- the sheet --------------------------------------------------------------


def test_sheet_defaults_and_derived_stats() -> None:
    sheet = CharacterSheet(name="Bran")
    assert sheet.abilities == {ability: 10 for ability in ABILITIES}
    assert sheet.level == 1
    assert sheet.proficiency == 1
    assert sheet.modifier("strength") == 0
    assert sheet.save_bonus("strength") == 1
    assert (sheet.hp, sheet.max_hp, sheet.ac) == (10, 10, 10)
    assert (sheet.gold, sheet.xp, sheet.inventory) == (0, 0, {})
    assert sheet.max_spell_slots == sheet.spell_slots == {}


def test_sheet_derives_level_and_bonuses_from_xp() -> None:
    sheet = CharacterSheet(name="Bran", xp=6500)  # level 5 -> proficiency 2
    sheet.abilities["dexterity"] = 14  # modifier +2
    assert sheet.level == 5
    assert sheet.proficiency == 2
    assert sheet.modifier("dexterity") == 2
    assert sheet.save_bonus("dexterity") == 4


def test_sheet_modifier_rejects_unknown_ability() -> None:
    with pytest.raises(ValueError, match="unknown ability"):
        CharacterSheet(name="Bran").modifier("luck")


def test_sheet_requires_a_name() -> None:
    with pytest.raises(ValueError, match="name must be non-empty"):
        CharacterSheet(name="")


def test_sheet_validates_hp_window() -> None:
    with pytest.raises(ValueError, match="max_hp must be >= 1"):
        CharacterSheet(name="Bran", max_hp=0)
    with pytest.raises(ValueError, match="hp must be 0..max_hp"):
        CharacterSheet(name="Bran", hp=-1)
    with pytest.raises(ValueError, match="hp must be 0..max_hp"):
        CharacterSheet(name="Bran", hp=11, max_hp=10)


def test_sheet_validates_ac_gold_xp() -> None:
    with pytest.raises(ValueError, match="ac must be >= 1"):
        CharacterSheet(name="Bran", ac=0)
    with pytest.raises(ValueError, match="gold cannot be negative"):
        CharacterSheet(name="Bran", gold=-1)
    with pytest.raises(ValueError, match="xp cannot be negative"):
        CharacterSheet(name="Bran", xp=-1)


def test_sheet_validates_spellcasting_class_and_expended_slots() -> None:
    with pytest.raises(ValueError, match="spellcasting_class must be a string"):
        CharacterSheet(name="Bran", spellcasting_class=7)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unsupported spellcasting class"):
        CharacterSheet(name="Bran", spellcasting_class="fighter")
    with pytest.raises(ValueError, match="must be a mapping"):
        CharacterSheet(name="Bran", expended_spell_slots=[])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="spell slot level must be 1..9"):
        CharacterSheet(name="Bran", spellcasting_class="wizard", expended_spell_slots={True: 1})
    with pytest.raises(ValueError, match="spell slot level must be 1..9"):
        CharacterSheet(name="Bran", spellcasting_class="wizard", expended_spell_slots={"x": 1})
    with pytest.raises(ValueError, match="spell slot level must be 1..9"):
        CharacterSheet(name="Bran", spellcasting_class="wizard", expended_spell_slots={1.5: 1})
    with pytest.raises(ValueError, match="spell slot level must be 1..9"):
        CharacterSheet(name="Bran", spellcasting_class="wizard", expended_spell_slots={"1.0": 1})
    with pytest.raises(ValueError, match="spell slot level must be 1..9"):
        CharacterSheet(name="Bran", spellcasting_class="wizard", expended_spell_slots={10: 1})
    with pytest.raises(ValueError, match="positive int"):
        CharacterSheet(name="Bran", spellcasting_class="wizard", expended_spell_slots={1: True})
    with pytest.raises(ValueError, match="positive int"):
        CharacterSheet(name="Bran", spellcasting_class="wizard", expended_spell_slots={1: 0})
    with pytest.raises(ValueError, match="maximum is 0"):
        CharacterSheet(name="Bran", expended_spell_slots={1: 1})
    with pytest.raises(ValueError, match="maximum is 2"):
        CharacterSheet(
            name="Bran", spellcasting_class="wizard", expended_spell_slots={1: 3}
        )


def test_sheet_validates_abilities() -> None:
    with pytest.raises(ValueError, match="unknown ability"):
        CharacterSheet(name="Bran", abilities={"strength": 10, "luck": 15})
    with pytest.raises(ValueError, match="ability score out of range"):
        CharacterSheet(name="Bran", abilities={"strength": 1})


def test_sheet_validates_inventory() -> None:
    with pytest.raises(ValueError, match="item name must be non-empty"):
        CharacterSheet(name="Bran", inventory={"": 1})
    with pytest.raises(ValueError, match="must be >= 1"):
        CharacterSheet(name="Bran", inventory={"torch": 0})


# -- mutations --------------------------------------------------------------


def test_sheet_take_damage_clamps_at_zero() -> None:
    sheet = CharacterSheet(name="Bran")
    assert sheet.take_damage(4) == 6
    assert sheet.take_damage(99) == 0
    with pytest.raises(ValueError, match="damage cannot be negative"):
        sheet.take_damage(-1)


def test_sheet_heal_clamps_at_max_hp() -> None:
    sheet = CharacterSheet(name="Bran", hp=0)
    assert sheet.heal(4) == 4
    assert sheet.heal(99) == 10
    with pytest.raises(ValueError, match="heal amount cannot be negative"):
        sheet.heal(-1)


def test_sheet_add_xp_levels_up() -> None:
    sheet = CharacterSheet(name="Bran", xp=300)  # level 2
    assert sheet.add_xp(6000) == 4  # 6300 xp -> level 4 (6500 needed for 5)
    assert sheet.level == 4
    assert sheet.proficiency == 1  # levels 1-4: +1
    with pytest.raises(ValueError, match="xp amount cannot be negative"):
        sheet.add_xp(-1)


def test_sheet_spends_slots_and_level_up_increases_the_derived_pool() -> None:
    sheet = CharacterSheet(name="Bran", spellcasting_class="wizard")
    assert sheet.spend_spell_slot(1) == 1
    assert sheet.spell_slots == {1: 1}
    assert sheet.add_xp(300) == 2
    assert sheet.max_spell_slots == {1: 3}
    assert sheet.spell_slots == {1: 2}
    assert sheet.spend_spell_slot(1) == 1
    assert sheet.spend_spell_slot(1) == 0
    with pytest.raises(ValueError, match="no level 1 spell slots remain"):
        sheet.spend_spell_slot(1)
    with pytest.raises(ValueError, match="no level 2 spell slot is available"):
        sheet.spend_spell_slot(2)
    with pytest.raises(ValueError, match="spell slot level must be an int"):
        sheet.spend_spell_slot(True)  # type: ignore[arg-type]


def test_sheet_rest_recovers_slots_by_class_and_rest_type() -> None:
    wizard = CharacterSheet(name="Mira", hp=4, max_hp=10, spellcasting_class="wizard")
    wizard.spend_spell_slot(1)
    assert wizard.can_restore_spell_slots("long") is True
    assert wizard.can_restore_spell_slots("short") is False
    assert wizard.rest("short") == (3, 0)
    assert wizard.spell_slots == {1: 1}
    assert wizard.rest("long") == (3, 1)
    assert wizard.spell_slots == {1: 2}

    warlock = CharacterSheet(name="Vex", spellcasting_class="warlock")
    warlock.spend_spell_slot(1)
    assert warlock.can_restore_spell_slots("short") is True
    assert warlock.rest("short") == (0, 1)
    assert warlock.spell_slots == {1: 1}
    with pytest.raises(ValueError, match="rest_type must be short|long"):
        warlock.can_restore_spell_slots("overnight")
    with pytest.raises(ValueError, match="rest_type must be short|long"):
        warlock.rest("overnight")


def test_sheet_gold_never_drops_below_zero() -> None:
    sheet = CharacterSheet(name="Bran", gold=5)
    assert sheet.add_gold(10) == 15
    assert sheet.add_gold(-3) == 12
    with pytest.raises(ValueError, match="cannot spend 20 gold"):
        sheet.add_gold(-20)


def test_sheet_items_stack_and_deplete() -> None:
    sheet = CharacterSheet(name="Bran")
    assert sheet.add_item("torch") == 1
    assert sheet.add_item("torch") == 2
    assert sheet.add_item("rations", 3) == 3
    assert sheet.remove_item("torch") == 1
    assert sheet.remove_item("torch") == 0
    assert "torch" not in sheet.inventory
    with pytest.raises(ValueError, match="only have 3"):
        sheet.remove_item("rations", 4)
    with pytest.raises(ValueError, match="only have 0"):
        sheet.remove_item("sword")
    with pytest.raises(ValueError, match="quantity must be"):
        sheet.add_item("torch", 0)
    with pytest.raises(ValueError, match="quantity must be"):
        sheet.remove_item("rations", 0)
    with pytest.raises(ValueError, match="item name must be non-empty"):
        sheet.add_item("")
