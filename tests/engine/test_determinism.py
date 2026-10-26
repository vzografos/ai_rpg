"""M1 acceptance (roadmap): `roll` output byte-identical under the same seed,
and a scripted encounter resolvable entirely by the engine (zero LLM, zero I/O)."""

from __future__ import annotations

from rpg.engine.combat import Combat, Combatant
from rpg.engine.dice import SeededSource, roll_dice
from rpg.engine.rules import ability_check

SEED = 7
MAX_TURNS = 1000  # guard: a bad seed must fail loudly, never hang the suite
FORMULAS = ("1d20", "2d6+3", "2d20kh1", "2d20kl1", "4d6kh2-1", "1d12+4")


def _roll_sequence(seed: int) -> list[str]:
    source = SeededSource(seed)
    return [str(roll_dice(formula, source)) for formula in FORMULAS] * 3


def test_roll_output_is_byte_identical_under_the_same_seed() -> None:
    # Two fresh, independent SeededSource instances: the canonical output of
    # every roll in the sequence must match character for character.
    assert _roll_sequence(SEED) == _roll_sequence(SEED)


def test_roll_output_differs_under_a_different_seed() -> None:
    assert _roll_sequence(SEED) != _roll_sequence(SEED + 1)


def test_roll_output_matches_hardcoded_literals() -> None:
    # Pinned across separate container runs (2026-10-25): the canonical form
    # of these rolls from a fresh seed-42 source is part of the M1 acceptance
    # contract (see also `python -m rpg.cli roll ... --seed 42`).
    assert (
        str(roll_dice("2d20kh1+3", SeededSource(42)))
        == "2d20kh1+3 rolled=[4, 1] kept=[4] total=7"
    )
    assert (
        str(roll_dice("4d6kh2-1", SeededSource(42)))
        == "4d6kh2-1 rolled=[6, 1, 1, 6] kept=[6, 6] total=11"
    )


def test_check_resolution_is_deterministic_under_the_same_seed() -> None:
    def run() -> list:
        source = SeededSource(SEED)
        return [ability_check(source, 2, 14, advantage=(i % 2 == 0)) for i in range(6)]

    assert run() == run()
    assert run() != [ability_check(SeededSource(SEED + 1), 2, 14)]


def _scripted_encounter(seed: int) -> tuple[str, Combat, Combatant]:
    """A full encounter played with a fixed deterministic policy: every
    combatant attacks the first living combatant ahead of them in initiative
    order, until all but one are dead."""
    source = SeededSource(seed)
    hero = Combatant(
        name="Hero", ac=16, hp=24, max_hp=24, dexterity=14, attack_bonus=5, damage="1d8+2"
    )
    goblin_a = Combatant(
        name="Goblin A", ac=13, hp=7, max_hp=7, dexterity=16, attack_bonus=4, damage="1d6+2"
    )
    goblin_b = Combatant(
        name="Goblin B", ac=15, hp=11, max_hp=11, dexterity=14, attack_bonus=3, damage="1d4+2"
    )
    combat = Combat(source, [hero, goblin_a, goblin_b])
    transcript = [f"initiative: {[c.name for c in combat.begin()]}"]
    turns = 0
    while not combat.is_over:
        turns += 1
        if turns > MAX_TURNS:
            raise AssertionError("encounter did not resolve within the turn guard")
        actor = combat.current
        target = next(c for c in combat.order if c is not actor and c.hp > 0)
        result = combat.attack(actor, target)
        transcript.append(
            f"{actor.name} -> {target.name}: d20={result.d20} to_hit={result.to_hit} "
            f"hit={result.hit} damage={result.damage}"
        )
        combat.end_turn()
    transcript.append(f"rounds: {combat.round}; hero hp: {hero.hp}")
    return "\n".join(transcript), combat, hero


def test_scripted_encounter_resolves_entirely_by_the_engine() -> None:
    transcript, combat, hero = _scripted_encounter(SEED)
    assert combat.is_over is True
    assert 0 <= hero.hp <= hero.max_hp
    assert "hit=True" in transcript  # at least one attack connected
    assert transcript.rsplit("\n", 1)[1].startswith("rounds:")


def test_scripted_encounter_is_byte_identical_under_the_same_seed() -> None:
    (first, _, _), (second, _, _) = _scripted_encounter(SEED), _scripted_encounter(SEED)
    assert first == second
    assert first != _scripted_encounter(SEED + 1)[0]
