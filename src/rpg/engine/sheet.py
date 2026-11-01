"""Character sheet (roadmap M1): derived stats, XP, and spell-slot tables.

Derived stats (SRD 5.1):
- ability modifier  = (score - 10) // 2         (scores 3..30)
- proficiency bonus = 1 + (level - 1) // 4      (levels 1..20)
- level             = derived from total XP (the table below)
- save/check bonus  = modifier + proficiency

The sheet is pure data: every mutation goes through a validated method
(hard constraint: the LLM never owns numbers).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "ABILITIES",
    "SPELLCASTING_CLASSES",
    "ability_modifier",
    "proficiency_bonus",
    "level_for_xp",
    "next_level_xp",
    "spell_slots_for",
    "xp_progress",
    "CharacterSheet",
]

ABILITIES = ("strength", "dexterity", "constitution", "intelligence", "wisdom", "charisma")

# Total XP required to reach each level (SRD 5.1).
XP_BY_LEVEL: dict[int, int] = {
    1: 0,
    2: 300,
    3: 900,
    4: 2700,
    5: 6500,
    6: 15000,
    7: 33000,
    8: 75000,
    9: 165000,
    10: 330000,
    11: 510000,
    12: 640000,
    13: 780000,
    14: 960000,
    15: 1200000,
    16: 1500000,
    17: 1900000,
    18: 2250000,
    19: 2650000,
    20: 3350000,
}

# SRD 5.1 full-caster slots by character level. Each row lists the number of
# slots for spell levels 1 through 9; trailing zeroes are omitted.
_FULL_CASTER_SLOTS: tuple[tuple[int, ...], ...] = (
    (2,),
    (3,),
    (4, 2),
    (4, 3),
    (4, 3, 2),
    (4, 3, 3),
    (4, 3, 3, 1),
    (4, 3, 3, 2),
    (4, 3, 3, 3, 1),
    (4, 3, 3, 3, 2),
    (4, 3, 3, 3, 2, 1),
    (4, 3, 3, 3, 2, 1),
    (4, 3, 3, 3, 2, 1, 1),
    (4, 3, 3, 3, 2, 1, 1),
    (4, 3, 3, 3, 2, 1, 1, 1),
    (4, 3, 3, 3, 2, 1, 1, 1),
    (4, 3, 3, 3, 2, 1, 1, 1, 1),
    (4, 3, 3, 3, 3, 1, 1, 1, 1),
    (4, 3, 3, 3, 3, 2, 1, 1, 1),
    (4, 3, 3, 3, 3, 2, 2, 1, 1),
)

# Pact Magic has a separate pool: every slot has the listed level and
# recovers on a short or long rest.
_WARLOCK_SLOTS: tuple[tuple[int, int], ...] = (
    (1, 1),
    (2, 1),
    (2, 2),
    (2, 2),
    (2, 3),
    (2, 3),
    (2, 4),
    (2, 4),
    (2, 5),
    (2, 5),
    (3, 5),
    (3, 5),
    (3, 5),
    (3, 5),
    (3, 5),
    (3, 5),
    (4, 5),
    (4, 5),
    (4, 5),
    (4, 5),
)

SPELLCASTING_CLASSES = frozenset(
    {"bard", "cleric", "druid", "paladin", "ranger", "sorcerer", "warlock", "wizard"}
)
_HALF_CASTER_SLOTS: tuple[tuple[int, ...], ...] = (
    (),
    (2,),
    (3,),
    (3,),
    (4, 2),
    (4, 2),
    (4, 3),
    (4, 3),
    (4, 3, 2),
    (4, 3, 2),
    (4, 3, 3),
    (4, 3, 3),
    (4, 3, 3, 1),
    (4, 3, 3, 1),
    (4, 3, 3, 2),
    (4, 3, 3, 2),
    (4, 3, 3, 3, 1),
    (4, 3, 3, 3, 1),
    (4, 3, 3, 3, 2),
    (4, 3, 3, 3, 2),
)


def ability_modifier(score: int) -> int:
    """(score - 10) // 2 (floor division); valid scores are 3..30."""
    if not 3 <= score <= 30:
        raise ValueError(f"ability score out of range 3..30: {score}")
    return (score - 10) // 2


def proficiency_bonus(level: int) -> int:
    """1 + (level - 1) // 4; valid levels are 1..20."""
    if not 1 <= level <= 20:
        raise ValueError(f"level out of range 1..20: {level}")
    return 1 + (level - 1) // 4


def level_for_xp(xp: int) -> int:
    """The level for a total XP (0 XP -> level 1; past level 20 -> 20)."""
    if xp < 0:
        raise ValueError(f"xp cannot be negative: {xp}")
    level = 1
    for lvl in range(2, 21):
        if xp >= XP_BY_LEVEL[lvl]:
            level = lvl
    return level


def next_level_xp(level: int) -> int | None:
    """XP required for the next level, or None at max level (20)."""
    if not 1 <= level <= 20:
        raise ValueError(f"level out of range 1..20: {level}")
    return None if level == 20 else XP_BY_LEVEL[level + 1]


def spell_slots_for(spellcasting_class: str | None, level: int) -> dict[int, int]:
    """Maximum spell slots by slot level for one SRD 5.1 class (levels 1..20).

    Bard, cleric, druid, sorcerer, and wizard use the full-caster table;
    paladin and ranger use their half-caster class table; warlock uses its
    separate Pact Magic pool. Multiclass and subclass progressions are
    outside this single-class M1 rules slice.
    """
    if isinstance(level, bool) or not isinstance(level, int) or not 1 <= level <= 20:
        raise ValueError(f"level out of range 1..20: {level!r}")
    if spellcasting_class is None:
        return {}
    if not isinstance(spellcasting_class, str):
        raise ValueError("spellcasting_class must be a string or None")
    normalized = spellcasting_class.strip().casefold()
    if normalized not in SPELLCASTING_CLASSES:
        raise ValueError(f"unsupported spellcasting class: {spellcasting_class!r}")
    if normalized == "warlock":
        count, slot_level = _WARLOCK_SLOTS[level - 1]
        return {slot_level: count}
    row = (
        _HALF_CASTER_SLOTS[level - 1]
        if normalized in {"paladin", "ranger"}
        else _FULL_CASTER_SLOTS[level - 1]
    )
    return {slot_level: count for slot_level, count in enumerate(row, start=1)}


def xp_progress(sheet: CharacterSheet) -> dict[str, Any]:
    """The scoring panel (roadmap M6): where the sheet stands on the XP table.

    Everything is derived from the stored `xp` — the sheet cannot drift from
    its own numbers (C3). At max level `next_level_xp`/`xp_to_next` are None.
    """
    level = sheet.level
    next_xp = next_level_xp(level)
    return {
        "level": level,
        "proficiency": sheet.proficiency,
        "xp": sheet.xp,
        "next_level_xp": next_xp,
        "xp_to_next": None if next_xp is None else max(0, next_xp - sheet.xp),
    }


@dataclass
class CharacterSheet:
    """A PC as pure, validated data.

    Derived stats (level, proficiency, modifiers, maximum spell slots) are
    computed properties — never stored, so they cannot drift from their
    source numbers. Expended slots are event-backed resource state.
    """

    name: str
    hp: int = 10
    max_hp: int = 10
    ac: int = 10
    abilities: dict[str, int] = field(default_factory=lambda: {a: 10 for a in ABILITIES})
    inventory: dict[str, int] = field(default_factory=dict)
    gold: int = 0
    xp: int = 0
    spellcasting_class: str | None = None
    expended_spell_slots: dict[int, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("character name must be non-empty")
        if self.max_hp < 1:
            raise ValueError(f"max_hp must be >= 1: {self.max_hp}")
        if not 0 <= self.hp <= self.max_hp:
            raise ValueError(f"hp must be 0..max_hp: {self.hp}")
        if self.ac < 1:
            raise ValueError(f"ac must be >= 1: {self.ac}")
        if self.gold < 0:
            raise ValueError(f"gold cannot be negative: {self.gold}")
        if self.xp < 0:
            raise ValueError(f"xp cannot be negative: {self.xp}")
        if self.spellcasting_class is not None:
            if not isinstance(self.spellcasting_class, str):
                raise ValueError("spellcasting_class must be a string or None")
            self.spellcasting_class = self.spellcasting_class.strip().casefold()
            if self.spellcasting_class not in SPELLCASTING_CLASSES:
                raise ValueError(
                    f"unsupported spellcasting class: {self.spellcasting_class!r}"
                )
        if not isinstance(self.expended_spell_slots, dict):
            raise ValueError("expended_spell_slots must be a mapping")
        expended: dict[int, int] = {}
        for raw_level, count in self.expended_spell_slots.items():
            if isinstance(raw_level, bool) or not isinstance(raw_level, (int, str)):
                raise ValueError(f"spell slot level must be 1..9: {raw_level!r}")
            if isinstance(raw_level, int):
                slot_level = raw_level
            elif raw_level.isdecimal():
                slot_level = int(raw_level)
            else:
                raise ValueError(f"spell slot level must be 1..9: {raw_level!r}")
            if not 1 <= slot_level <= 9:
                raise ValueError(f"spell slot level must be 1..9: {raw_level!r}")
            if isinstance(count, bool) or not isinstance(count, int) or count < 1:
                raise ValueError(f"expended spell slot count must be a positive int: {count!r}")
            expended[slot_level] = expended.get(slot_level, 0) + count
        self.expended_spell_slots = expended
        for slot_level, count in expended.items():
            maximum = self.max_spell_slots.get(slot_level, 0)
            if count > maximum:
                raise ValueError(
                    f"expended {count} level {slot_level} spell slots, but maximum is {maximum}"
                )
        for ability, score in self.abilities.items():
            if ability not in ABILITIES:
                raise ValueError(f"unknown ability: {ability!r}")
            ability_modifier(score)
        for item, qty in self.inventory.items():
            if not item:
                raise ValueError("item name must be non-empty")
            if qty < 1:
                raise ValueError(f"inventory qty for {item!r} must be >= 1: {qty}")

    @property
    def level(self) -> int:
        return level_for_xp(self.xp)

    @property
    def proficiency(self) -> int:
        return proficiency_bonus(self.level)

    @property
    def max_spell_slots(self) -> dict[int, int]:
        """The class/level-derived spell slot capacity."""
        return spell_slots_for(self.spellcasting_class, self.level)

    @property
    def spell_slots(self) -> dict[int, int]:
        """Available slots by spell level; spent slots are event-backed data."""
        return {
            slot_level: count - self.expended_spell_slots.get(slot_level, 0)
            for slot_level, count in self.max_spell_slots.items()
        }

    def spend_spell_slot(self, slot_level: int) -> int:
        """Spend one available slot; return the remaining slots at that level."""
        if isinstance(slot_level, bool) or not isinstance(slot_level, int):
            raise ValueError(f"spell slot level must be an int: {slot_level!r}")
        maximum = self.max_spell_slots.get(slot_level)
        if maximum is None:
            raise ValueError(f"no level {slot_level} spell slot is available")
        expended = self.expended_spell_slots.get(slot_level, 0)
        if expended >= maximum:
            raise ValueError(f"no level {slot_level} spell slots remain")
        self.expended_spell_slots[slot_level] = expended + 1
        return maximum - expended - 1

    def can_restore_spell_slots(self, rest_type: str) -> bool:
        """Whether this rest would recover any currently expended slots."""
        if rest_type not in ("short", "long"):
            raise ValueError(f"rest_type must be short|long: {rest_type!r}")
        can_recover = rest_type == "long" or (
            self.spellcasting_class == "warlock" and rest_type == "short"
        )
        return can_recover and bool(self.expended_spell_slots)

    def rest(self, rest_type: str) -> tuple[int, int]:
        """Apply this project's short/long rest HP and spell-slot recovery.

        Returns (HP recovered, spell slots recovered). A long rest restores
        ordinary slots; Pact Magic also returns on a short rest. Existing
        project HP semantics are retained: long rest heals to full, short
        rest heals half the missing HP, rounded up.
        """
        if rest_type not in ("short", "long"):
            raise ValueError(f"rest_type must be short|long: {rest_type!r}")
        missing = self.max_hp - self.hp
        healed = missing if rest_type == "long" else -(-missing // 2)
        self.heal(healed)
        slots_before = sum(self.spell_slots.values())
        if self.can_restore_spell_slots(rest_type):
            self.expended_spell_slots.clear()
        slots_recovered = sum(self.spell_slots.values()) - slots_before
        return healed, slots_recovered

    def modifier(self, ability: str) -> int:
        """The ability modifier ((score - 10) // 2) for a known ability."""
        try:
            score = self.abilities[ability]
        except KeyError:
            raise ValueError(f"unknown ability: {ability!r}") from None
        return ability_modifier(score)

    def save_bonus(self, ability: str) -> int:
        """The save/check bonus: ability modifier + proficiency."""
        return self.modifier(ability) + self.proficiency

    def add_xp(self, amount: int) -> int:
        """Grant XP (leveling up per the table); returns the new level."""
        if amount < 0:
            raise ValueError(f"xp amount cannot be negative: {amount}")
        self.xp += amount
        return self.level

    def take_damage(self, amount: int) -> int:
        """Apply damage; hp clamps at 0 (death saves live in combat)."""
        if amount < 0:
            raise ValueError(f"damage cannot be negative: {amount}")
        self.hp = max(0, self.hp - amount)
        return self.hp

    def heal(self, amount: int) -> int:
        """Heal; hp clamps at max_hp."""
        if amount < 0:
            raise ValueError(f"heal amount cannot be negative: {amount}")
        self.hp = min(self.max_hp, self.hp + amount)
        return self.hp

    def add_gold(self, amount: int) -> int:
        """Add (positive) or spend (negative) gold; never drops below 0."""
        if self.gold + amount < 0:
            raise ValueError(f"cannot spend {abs(amount)} gold; only have {self.gold}")
        self.gold += amount
        return self.gold

    def add_item(self, name: str, quantity: int = 1) -> int:
        """Add an item (stacks); returns the new quantity."""
        if not name:
            raise ValueError("item name must be non-empty")
        if quantity < 1:
            raise ValueError(f"quantity must be >= 1: {quantity}")
        self.inventory[name] = self.inventory.get(name, 0) + quantity
        return self.inventory[name]

    def remove_item(self, name: str, quantity: int = 1) -> int:
        """Remove an item (validated against the inventory); returns the rest."""
        if quantity < 1:
            raise ValueError(f"quantity must be >= 1: {quantity}")
        owned = self.inventory.get(name, 0)
        if owned < quantity:
            raise ValueError(f"cannot remove {quantity}x {name!r}; only have {owned}")
        self.inventory[name] = owned - quantity
        if self.inventory[name] == 0:
            del self.inventory[name]
        return self.inventory.get(name, 0)
