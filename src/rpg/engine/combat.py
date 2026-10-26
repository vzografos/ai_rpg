"""Combat subset (roadmap M1): initiative, attack resolution, HP, conditions.

- initiative order (d20 + Dex modifier, ties by name)
- attack resolution: to-hit d20 + mod vs AC, damage dice, crits (2x dice)
- conditions: poisoned, stunned, prone, unconscious
- death saves; death at 0 HP

Pure and deterministic given a RandomSource — no LLM, no I/O.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from rpg.engine.dice import RandomSource, parse_formula, roll_dice
from rpg.engine.sheet import ability_modifier

__all__ = ["CONDITIONS", "Combatant", "Combat", "AttackResult", "DamageResult"]

CONDITIONS = ("poisoned", "stunned", "prone", "unconscious")

# 5e: an unconscious creature is also (blinded,) stunned and prone — the
# first two are modelled here as effects that unconscious implies.
_UNCONSCIOUS_IMPLIES = ("stunned", "prone")


def _has_effect(combatant: Combatant, effect: str) -> bool:
    if effect in combatant.conditions:
        return True
    return effect in _UNCONSCIOUS_IMPLIES and "unconscious" in combatant.conditions


@dataclass
class Combatant:
    """A creature in combat. Pure data; only Combat mutates it."""

    name: str
    ac: int
    hp: int
    max_hp: int
    dexterity: int  # the score (3..30); the modifier is derived
    attack_bonus: int
    damage: str  # damage formula, e.g. "1d8+2"
    conditions: set[str] = field(default_factory=set)
    initiative: int | None = None
    death_successes: int = 0
    death_failures: int = 0

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("combatant name must be non-empty")
        if self.max_hp < 1:
            raise ValueError(f"max_hp must be >= 1: {self.max_hp}")
        if not 0 <= self.hp <= self.max_hp:
            raise ValueError(f"hp must be 0..max_hp: {self.hp}")
        if self.ac < 1:
            raise ValueError(f"ac must be >= 1: {self.ac}")
        ability_modifier(self.dexterity)
        parse_formula(self.damage)
        unknown = self.conditions - set(CONDITIONS)
        if unknown:
            raise ValueError(f"unknown condition(s): {sorted(unknown)!r}")

    @property
    def is_dead(self) -> bool:
        """Dead: 0 HP with three death-save failures (5e)."""
        return self.hp == 0 and self.death_failures >= 3


@dataclass(frozen=True)
class DamageResult:
    hp: int
    death_save: int | None  # the d20, if one was rolled
    death_save_success: bool | None
    stable: bool
    dead: bool


@dataclass(frozen=True)
class AttackResult:
    attacker: str
    defender: str
    d20: int
    to_hit: int
    ac: int
    advantage: bool
    disadvantage: bool
    critical: bool
    fumble: bool
    hit: bool
    damage: int  # 0 on a miss
    outcome: DamageResult | None  # None when the attack missed


class Combat:
    """One encounter. Deterministic given its RandomSource.

    Combatants may be added until `begin()` rolls initiative; afterwards
    the roster is frozen.
    """

    def __init__(self, source: RandomSource, participants: Sequence[Combatant] = ()) -> None:
        self._source = source
        self._participants = list(participants)
        if len(self._participants) < 2:
            raise ValueError("an encounter needs at least 2 combatants")
        names = [c.name for c in self._participants]
        if len(set(names)) != len(names):
            raise ValueError("combatant names must be unique")
        self._order: list[Combatant] = []
        self._index = -1
        self._round = 0

    # -- roster ---------------------------------------------------------

    @property
    def participants(self) -> list[Combatant]:
        return list(self._participants)

    def add(self, combatant: Combatant) -> None:
        """Add a participant; forbidden once initiative has been rolled."""
        if self._order:
            raise ValueError("cannot add combatants after initiative is rolled")
        if combatant.name in {c.name for c in self._participants}:
            raise ValueError(f"duplicate combatant name: {combatant.name!r}")
        self._participants.append(combatant)

    # -- initiative -----------------------------------------------------

    def begin(self) -> list[Combatant]:
        """Roll initiative for everyone (1d20 + Dex modifier; ties broken
        alphabetically by name) and return the turn order."""
        if self._order:
            raise ValueError("initiative already rolled")
        for combatant in self._participants:
            d20 = roll_dice("1d20", self._source).kept[0]
            combatant.initiative = d20 + ability_modifier(combatant.dexterity)
        self._order = sorted(self._participants, key=lambda c: (-c.initiative, c.name))
        self._index = -1
        self._round = 0
        self._next_actionable()
        return list(self._order)

    @property
    def order(self) -> list[Combatant]:
        self._require_begun()
        return list(self._order)

    @property
    def round(self) -> int:
        self._require_begun()
        return self._round

    @property
    def current(self) -> Combatant:
        self._require_begun()
        return self._order[self._index]

    def end_turn(self) -> Combatant:
        """Pass the turn to the next combatant that can still act. A
        combatant at 0 HP (unconscious, or dead) does not take turns (5e).
        Wrapping around starts a new round."""
        self._require_begun()
        self._next_actionable()
        return self.current

    @property
    def is_over(self) -> bool:
        """Over when at most one combatant is still alive (5e: 0 HP is
        unconscious, and a stable or dead combatant is out of the fight)."""
        alive = sum(1 for c in self._participants if c.hp > 0)
        return alive <= 1

    def can_act(self, combatant: Combatant) -> bool:
        """5e: a creature at 0 HP is unconscious; stunned (or unconscious)
        creatures cannot take actions."""
        return combatant.hp > 0 and not _has_effect(combatant, "stunned")

    def _next_actionable(self) -> None:
        while True:
            self._index = (self._index + 1) % len(self._order)
            if self._index == 0:
                self._round += 1
            if self._order[self._index].hp > 0:
                return
            if not any(c.hp > 0 for c in self._order):
                raise ValueError("no living combatant left to act")

    def _require_begun(self) -> None:
        if not self._order:
            raise ValueError("combat has not begun (call begin() first)")

    def _check_member(self, combatant: Combatant) -> None:
        if combatant not in self._participants:
            raise ValueError(f"not in this combat: {combatant.name!r}")

    # -- conditions -----------------------------------------------------

    def add_condition(self, combatant: Combatant, condition: str) -> None:
        """Apply one of CONDITIONS to a participant (validated)."""
        self._check_member(combatant)
        if condition not in CONDITIONS:
            raise ValueError(f"unknown condition: {condition!r}")
        combatant.conditions.add(condition)

    def remove_condition(self, combatant: Combatant, condition: str) -> None:
        """Remove a condition (no-op if absent — e.g. standing up ends prone)."""
        self._check_member(combatant)
        if condition not in CONDITIONS:
            raise ValueError(f"unknown condition: {condition!r}")
        combatant.conditions.discard(condition)

    # -- HP / death saves -------------------------------------------------

    def apply_damage(self, combatant: Combatant, amount: int) -> DamageResult:
        """Damage a participant (clamped at 0 HP), rolling a death save
        whenever the hit drops them to 0 or keeps them there (5e)."""
        if amount < 0:
            raise ValueError(f"damage cannot be negative: {amount}")
        self._check_member(combatant)
        combatant.hp = max(0, combatant.hp - amount)
        if combatant.hp > 0:
            combatant.death_successes = 0
            combatant.death_failures = 0
            return DamageResult(
                hp=combatant.hp, death_save=None, death_save_success=None, stable=False, dead=False
            )
        if combatant.death_successes >= 3:
            return DamageResult(
                hp=0, death_save=None, death_save_success=None, stable=True, dead=False
            )
        death_save, success = self._roll_death_save(combatant)
        dead = combatant.death_failures >= 3
        stable = combatant.death_successes >= 3
        return DamageResult(
            hp=combatant.hp,
            death_save=death_save,
            death_save_success=success,
            stable=stable,
            dead=dead,
        )

    def _roll_death_save(self, combatant: Combatant) -> tuple[int, bool]:
        """d20 vs 10: natural 20 -> success + back to 1 HP; natural 1 ->
        failure; 10+ -> success; below 10 -> failure."""
        d20 = roll_dice("1d20", self._source).kept[0]
        if d20 == 20:
            combatant.death_successes += 1
            combatant.death_failures = 0
            combatant.hp = 1
            return d20, True
        if d20 == 1:
            combatant.death_failures += 1
            return d20, False
        if d20 >= 10:
            combatant.death_successes += 1
            return d20, True
        combatant.death_failures += 1
        return d20, False

    def heal(self, combatant: Combatant, amount: int) -> int:
        """Heal a participant (clamped at max HP); positive HP clears any
        pending death saves (5e)."""
        if amount < 0:
            raise ValueError(f"heal amount cannot be negative: {amount}")
        self._check_member(combatant)
        combatant.hp = min(combatant.max_hp, combatant.hp + amount)
        if combatant.hp > 0:
            combatant.death_successes = 0
            combatant.death_failures = 0
        return combatant.hp

    # -- attacks ----------------------------------------------------------

    def attack(
        self, attacker: Combatant, defender: Combatant, *, melee: bool = True
    ) -> AttackResult:
        """Resolve an attack: to-hit d20 + bonus vs AC, then damage.

        Condition effects (5e subset): a prone defender gives melee attacks
        advantage (and makes ranged attacks miss); a stunned/unconscious
        defender gives any attack advantage; a prone or poisoned attacker
        has disadvantage; advantage and disadvantage cancel.
        """
        self._check_member(attacker)
        self._check_member(defender)
        if attacker.is_dead:
            raise ValueError(f"a dead combatant cannot attack: {attacker.name!r}")
        if defender.is_dead:
            raise ValueError(f"cannot attack a dead combatant: {defender.name!r}")
        if attacker.hp <= 0:
            raise ValueError(
                f"a combatant at 0 HP is unconscious and cannot attack: {attacker.name!r}"
            )
        advantage = (melee and _has_effect(defender, "prone")) or _has_effect(
            defender, "stunned"
        )
        disadvantage = _has_effect(attacker, "prone") or _has_effect(attacker, "poisoned")
        if advantage and disadvantage:
            formula = "1d20"
        elif advantage:
            formula = "2d20kh1"
        elif disadvantage:
            formula = "2d20kl1"
        else:
            formula = "1d20"
        d20 = roll_dice(formula, self._source).kept[0]
        to_hit = d20 + attacker.attack_bonus
        critical = d20 == 20
        fumble = d20 == 1
        if not melee and _has_effect(defender, "prone"):
            hit = False  # 5e: a ranged attack misses a prone target
        else:
            hit = critical or (not fumble and to_hit >= defender.ac)
        damage = 0
        outcome: DamageResult | None = None
        if hit:
            damage_roll = roll_dice(attacker.damage, self._source)
            if critical:
                damage = 2 * sum(damage_roll.kept) + damage_roll.modifier
            else:
                damage = damage_roll.total
            outcome = self.apply_damage(defender, damage)
        return AttackResult(
            attacker=attacker.name,
            defender=defender.name,
            d20=d20,
            to_hit=to_hit,
            ac=defender.ac,
            advantage=advantage,
            disadvantage=disadvantage,
            critical=critical,
            fumble=fumble,
            hit=hit,
            damage=damage,
            outcome=outcome,
        )
