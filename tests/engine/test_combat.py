"""Combat subset (roadmap M1): initiative, attacks, conditions, death saves."""

from __future__ import annotations

import pytest

from rpg.engine.combat import Combat, Combatant, DamageResult


class CannedSource:
    """Pre-canned dice, one at a time (the parser rolls die by die)."""

    def __init__(self, values: list[int]) -> None:
        self.values = list(values)

    def roll(self, n: int, sides: int) -> int:
        assert n == 1
        return self.values.pop(0)


def make(name: str = "Hero", **kwargs) -> Combatant:
    defaults = dict(ac=15, hp=20, max_hp=20, dexterity=14, attack_bonus=5, damage="1d8+2")
    defaults.update(kwargs)
    return Combatant(name=name, **defaults)


# -- combatant validation ----------------------------------------------------


def test_combatant_validates_fields() -> None:
    with pytest.raises(ValueError, match="name must be non-empty"):
        make(name="")
    with pytest.raises(ValueError, match="max_hp must be >= 1"):
        make(max_hp=0)
    with pytest.raises(ValueError, match="hp must be 0..max_hp"):
        make(hp=21)
    with pytest.raises(ValueError, match="ac must be >= 1"):
        make(ac=0)
    with pytest.raises(ValueError, match="ability score out of range"):
        make(dexterity=1)
    with pytest.raises(ValueError, match="invalid dice formula"):
        make(damage="2d")
    with pytest.raises(ValueError, match="unknown condition"):
        make(conditions={"paralyzed"})


def test_combatant_is_dead_only_at_zero_hp_with_three_failures() -> None:
    assert make(hp=0).is_dead is False
    assert make(hp=0, death_failures=2).is_dead is False
    assert make(hp=0, death_failures=3).is_dead is True
    assert make(hp=1, death_failures=3).is_dead is False


# -- combat construction / initiative ------------------------------------------


def test_combat_needs_two_unique_combatants() -> None:
    with pytest.raises(ValueError, match="at least 2"):
        Combat(CannedSource([1]), [make("A")])
    with pytest.raises(ValueError, match="unique"):
        Combat(CannedSource([1, 1]), [make("A"), make("A")])


def test_initiative_uses_d20_plus_dex_modifier() -> None:
    a = make("Alpha", dexterity=16)  # +3
    b = make("Bravo", dexterity=10)  # +0
    order = Combat(CannedSource([10, 10]), [b, a]).begin()
    assert [c.name for c in order] == ["Alpha", "Bravo"]
    assert (a.initiative, b.initiative) == (13, 10)


def test_initiative_tie_breaks_alphabetically_by_name() -> None:
    a = make("Zeta")
    b = make("Alpha")
    order = Combat(CannedSource([10, 10]), [a, b]).begin()
    assert [c.name for c in order] == ["Alpha", "Zeta"]


def test_turn_order_cycles_and_counts_rounds() -> None:
    a = make("A", dexterity=18)
    b = make("B", dexterity=10)
    combat = Combat(CannedSource([10, 10]), [b, a])
    combat.begin()
    assert combat.current is a
    assert combat.round == 1
    assert combat.end_turn() is b
    assert combat.current is b
    assert combat.round == 1
    assert combat.end_turn() is a
    assert combat.round == 2
    assert combat.order == [a, b]


def test_initiative_accessors_raise_before_begin() -> None:
    combat = Combat(CannedSource([1]), [make("A"), make("B")])
    with pytest.raises(ValueError, match="not begun"):
        _ = combat.order
    with pytest.raises(ValueError, match="not begun"):
        _ = combat.current
    with pytest.raises(ValueError, match="not begun"):
        _ = combat.round
    with pytest.raises(ValueError, match="not begun"):
        combat.end_turn()


def test_begin_twice_is_rejected() -> None:
    combat = Combat(CannedSource([10, 10, 10, 10]), [make("A"), make("B")])
    combat.begin()
    with pytest.raises(ValueError, match="already rolled"):
        combat.begin()


def test_begin_raises_when_no_combatant_can_act() -> None:
    combat = Combat(CannedSource([1, 1]), [make("A", hp=0), make("B", hp=0)])
    with pytest.raises(ValueError, match="no living combatant"):
        combat.begin()


def test_end_turn_raises_when_no_combatant_can_act() -> None:
    a = make("A")
    b = make("B")
    combat = Combat(CannedSource([1, 1]), [a, b])
    combat.begin()
    a.hp = 0
    b.hp = 0
    with pytest.raises(ValueError, match="no living combatant"):
        combat.end_turn()


def test_add_participant_only_before_initiative() -> None:
    combat = Combat(CannedSource([1, 1, 1]), [make("A"), make("B")])
    combat.add(make("C", dexterity=18))
    assert {c.name for c in combat.participants} == {"A", "B", "C"}
    with pytest.raises(ValueError, match="duplicate"):
        combat.add(make("B"))
    combat.begin()
    with pytest.raises(ValueError, match="after initiative"):
        combat.add(make("D"))


def test_is_over_when_all_but_one_are_dead() -> None:
    living = make("Hero")
    dead = make("Goblin", hp=0, death_failures=3)
    assert Combat(CannedSource([1]), [living, dead]).is_over is True
    assert Combat(CannedSource([1]), [make("A"), make("B")]).is_over is False


def test_is_over_ignores_stable_combatants() -> None:
    living = make("Hero")
    stable = make("Goblin", hp=0, death_successes=3)
    assert Combat(CannedSource([1]), [living, stable]).is_over is True


def test_end_turn_skips_unconscious_combatants() -> None:
    a = make("A")
    b = make("B", hp=0)  # unconscious: no more turns
    c = make("C")
    combat = Combat(CannedSource([1, 1, 1]), [a, b, c])
    combat.begin()
    assert combat.current is a
    assert combat.end_turn() is c  # B's turn is skipped
    assert combat.end_turn() is a  # wraps around: round 2
    assert combat.round == 2


def test_attack_rejects_an_unconscious_attacker() -> None:
    hero = make("Hero")
    unconscious = make("Unconscious", hp=0)
    combat = Combat(CannedSource([1]), [hero, unconscious])
    with pytest.raises(ValueError, match="unconscious and cannot attack"):
        combat.attack(unconscious, hero)


def test_can_act_is_false_at_zero_hp() -> None:
    combat = Combat(CannedSource([1]), [make("Hero"), make("Goblin")])
    goblin = combat.participants[1]
    assert combat.can_act(goblin) is True
    goblin.hp = 0
    assert combat.can_act(goblin) is False


# -- attacks --------------------------------------------------------------------


def test_attack_hit_deals_roll_plus_modifier() -> None:
    hero = make("Hero")
    goblin = make("Goblin", ac=14, hp=12)
    combat = Combat(CannedSource([17, 6]), [hero, goblin])
    result = combat.attack(hero, goblin)
    assert (result.d20, result.to_hit, result.ac) == (17, 22, 14)
    assert (result.hit, result.damage) == (True, 8)  # 6 + the +2 modifier
    expected = DamageResult(
        hp=4, death_save=None, death_save_success=None, stable=False, dead=False
    )
    assert result.outcome == expected
    assert (result.advantage, result.disadvantage, result.critical, result.fumble) == (
        False,
        False,
        False,
        False,
    )
    assert goblin.hp == 4


def test_attack_miss_deals_no_damage() -> None:
    hero = make("Hero")
    goblin = make("Goblin", ac=18)
    combat = Combat(CannedSource([5]), [hero, goblin])
    result = combat.attack(hero, goblin)
    assert (result.d20, result.to_hit, result.hit) == (5, 10, False)
    assert (result.damage, result.outcome) == (0, None)
    assert goblin.hp == goblin.max_hp


def test_natural_20_is_an_auto_hit_and_crits_double_the_dies() -> None:
    hero = make("Hero")
    goblin = make("Goblin", ac=30)  # even a 25 to-hit would miss
    combat = Combat(CannedSource([20, 6, 6]), [hero, goblin])
    result = combat.attack(hero, goblin)
    assert (result.critical, result.hit) == (True, True)
    assert result.damage == 2 * 6 + 2  # 1d8 twice, modifier once
    assert goblin.hp == 20 - 14


def test_multi_die_damage_crits_double_every_die() -> None:
    hero = make("Hero", damage="2d6+1")
    goblin = make("Goblin")
    combat = Combat(CannedSource([20, 3, 5]), [hero, goblin])
    assert combat.attack(hero, goblin).damage == 2 * (3 + 5) + 1


def test_natural_1_is_an_auto_miss() -> None:
    hero = make("Hero")
    goblin = make("Goblin", ac=1)
    combat = Combat(CannedSource([1]), [hero, goblin])
    result = combat.attack(hero, goblin)
    assert (result.fumble, result.hit, result.damage) == (True, False, 0)


def test_attack_rejects_dead_and_outsiders() -> None:
    hero = make("Hero")
    dead = make("Dead", hp=0, death_failures=3)
    combat = Combat(CannedSource([1]), [hero, dead])
    with pytest.raises(ValueError, match="cannot attack a dead combatant"):
        combat.attack(hero, dead)
    with pytest.raises(ValueError, match="dead combatant cannot attack"):
        combat.attack(dead, hero)
    outsider = make("Outsider")
    with pytest.raises(ValueError, match="not in this combat"):
        combat.attack(outsider, hero)
    with pytest.raises(ValueError, match="not in this combat"):
        combat.attack(hero, outsider)


# -- condition effects ------------------------------------------------------------


def test_prone_defender_gives_melee_advantage() -> None:
    hero = make("Hero")
    goblin = make("Goblin", conditions={"prone"})
    combat = Combat(CannedSource([3, 17, 6]), [hero, goblin])  # 2d20kh1, then 1d8
    result = combat.attack(hero, goblin)
    assert (result.advantage, result.d20, result.hit) == (True, 17, True)


def test_prone_defender_makes_ranged_miss() -> None:
    hero = make("Hero")
    goblin = make("Goblin", conditions={"prone"})
    combat = Combat(CannedSource([20]), [hero, goblin])
    result = combat.attack(hero, goblin, melee=False)
    assert (result.advantage, result.hit, result.damage) == (False, False, 0)


def test_prone_attacker_has_disadvantage() -> None:
    hero = make("Hero", conditions={"prone"})
    goblin = make("Goblin")
    combat = Combat(CannedSource([17, 3]), [hero, goblin])
    result = combat.attack(hero, goblin)
    assert (result.disadvantage, result.d20) == (True, 3)


def test_stunned_defender_gives_advantage_and_cannot_act() -> None:
    hero = make("Hero")
    goblin = make("Goblin", conditions={"stunned"})
    combat = Combat(CannedSource([3, 17, 6]), [hero, goblin])  # 2d20kh1, then 1d8
    assert combat.attack(hero, goblin).advantage is True
    assert (combat.can_act(goblin), combat.can_act(hero)) == (False, True)


def test_unconscious_defender_implies_stunned_and_prone() -> None:
    hero = make("Hero")
    goblin = make("Goblin", conditions={"unconscious"})
    combat = Combat(CannedSource([3, 17, 6]), [hero, goblin])  # 2d20kh1, then 1d8
    result = combat.attack(hero, goblin)
    assert (result.advantage, result.d20, combat.can_act(goblin)) == (True, 17, False)


def test_poisoned_attacker_has_disadvantage() -> None:
    hero = make("Hero", conditions={"poisoned"})
    goblin = make("Goblin")
    combat = Combat(CannedSource([17, 3]), [hero, goblin])
    result = combat.attack(hero, goblin)
    assert (result.disadvantage, result.d20) == (True, 3)


def test_advantage_and_disadvantage_cancel() -> None:
    hero = make("Hero", conditions={"prone"})
    goblin = make("Goblin", conditions={"stunned"})
    combat = Combat(CannedSource([9]), [hero, goblin])
    result = combat.attack(hero, goblin)
    assert (result.advantage, result.disadvantage, result.d20) == (True, True, 9)


def test_add_and_remove_condition() -> None:
    hero = make("Hero")
    combat = Combat(CannedSource([1]), [hero, make("Goblin")])
    combat.add_condition(hero, "prone")
    assert hero.conditions == {"prone"}
    combat.add_condition(hero, "prone")  # idempotent (it is a set)
    combat.remove_condition(hero, "prone")
    assert hero.conditions == set()
    combat.remove_condition(hero, "prone")  # no-op when absent
    with pytest.raises(ValueError, match="unknown condition"):
        combat.add_condition(hero, "paralyzed")
    with pytest.raises(ValueError, match="unknown condition"):
        combat.remove_condition(hero, "paralyzed")
    outsider = make("Outsider")
    with pytest.raises(ValueError, match="not in this combat"):
        combat.add_condition(outsider, "prone")
    with pytest.raises(ValueError, match="not in this combat"):
        combat.remove_condition(outsider, "prone")


# -- HP / death saves -----------------------------------------------------------


def test_damage_leaving_hp_positive_resets_death_saves() -> None:
    victim = make("Victim", hp=5, death_successes=1, death_failures=2)
    combat = Combat(CannedSource([1]), [make("Hero"), victim])
    outcome = combat.apply_damage(victim, 3)
    assert (outcome.hp, outcome.death_save, outcome.dead) == (2, None, False)
    assert (victim.death_successes, victim.death_failures) == (0, 0)


def test_death_save_natural_20_brings_back_to_one_hp() -> None:
    victim = make("Victim", hp=0)
    combat = Combat(CannedSource([20]), [make("Hero"), victim])
    outcome = combat.apply_damage(victim, 3)
    assert (outcome.hp, outcome.death_save, outcome.death_save_success, outcome.dead) == (
        1,
        20,
        True,
        False,
    )
    assert victim.hp == 1
    assert victim.death_failures == 0


def test_death_save_natural_1_and_low_rolls_fail() -> None:
    victim = make("Victim", hp=0)
    combat = Combat(CannedSource([1, 9]), [make("Hero"), victim])
    assert combat.apply_damage(victim, 1).death_save_success is False
    assert combat.apply_damage(victim, 1).death_save_success is False
    assert victim.death_failures == 2


def test_death_save_successes_stabilize_and_stop_rolling() -> None:
    victim = make("Victim", hp=0)
    combat = Combat(CannedSource([15, 12, 18]), [make("Hero"), victim])
    assert combat.apply_damage(victim, 1).death_save_success is True
    assert combat.apply_damage(victim, 1).death_save_success is True
    assert combat.apply_damage(victim, 1).death_save_success is True
    assert victim.death_successes == 3
    outcome = combat.apply_damage(victim, 1)
    assert (outcome.stable, outcome.death_save, outcome.dead) == (True, None, False)


def test_three_death_save_failures_kill() -> None:
    victim = make("Victim", hp=0)
    combat = Combat(CannedSource([1, 4, 8]), [make("Hero"), victim])
    assert combat.apply_damage(victim, 1).dead is False
    assert combat.apply_damage(victim, 1).dead is False
    outcome = combat.apply_damage(victim, 1)
    assert (outcome.dead, outcome.stable) == (True, False)
    assert victim.is_dead is True


def test_heal_clears_pending_death_saves() -> None:
    victim = make("Victim", hp=0, death_failures=2)
    combat = Combat(CannedSource([1]), [make("Hero"), victim])
    assert combat.heal(victim, 3) == 3
    assert (victim.death_successes, victim.death_failures) == (0, 0)
    assert victim.is_dead is False


def test_heal_clamps_at_max_hp_and_validates() -> None:
    victim = make("Victim", hp=15)
    combat = Combat(CannedSource([1]), [make("Hero"), victim])
    assert combat.heal(victim, 99) == 20
    with pytest.raises(ValueError, match="heal amount cannot be negative"):
        combat.heal(victim, -1)


def test_apply_damage_validates_amount_and_membership() -> None:
    combat = Combat(CannedSource([1]), [make("Hero"), make("Victim")])
    with pytest.raises(ValueError, match="damage cannot be negative"):
        combat.apply_damage(combat.participants[0], -1)
    with pytest.raises(ValueError, match="not in this combat"):
        combat.apply_damage(make("Outsider"), 1)


# -- a full death-by-attacks scenario ---------------------------------------------


def test_attacks_kill_through_death_saves() -> None:
    hero = make("Hero", damage="1d12+4")
    victim = make("Victim", hp=5)
    combat = Combat(CannedSource([15, 12, 5, 1, 20, 18, 4, 14, 7, 1]), [hero, victim])

    result = combat.attack(hero, victim)  # hit for 16 -> death save (5) fails
    assert (result.d20, result.damage) == (15, 16)
    assert result.outcome is not None
    assert (result.outcome.hp, result.outcome.death_save, result.outcome.dead) == (0, 5, False)
    assert (victim.hp, victim.death_failures) == (0, 1)
    assert victim.is_dead is False

    result = combat.attack(hero, victim)  # natural 1 -> auto miss
    assert (result.fumble, result.hit, result.damage) == (True, False, 0)
    assert result.outcome is None

    result = combat.attack(hero, victim)  # crit: 2x18 + 4 = 40 -> death save (4) fails
    assert (result.critical, result.damage) == (True, 40)
    assert result.outcome is not None
    assert (result.outcome.hp, result.outcome.death_save, result.outcome.dead) == (0, 4, False)
    assert victim.death_failures == 2

    result = combat.attack(hero, victim)  # hit for 11 -> natural 1 death save: death
    assert (result.hit, result.damage) == (True, 11)
    assert result.outcome is not None
    assert (result.outcome.death_save, result.outcome.dead) == (1, True)
    assert victim.is_dead is True
    assert combat.is_over is True
