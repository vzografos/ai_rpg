"""Character sheet (roadmap M1): abilities -> derived stats, XP table.

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

__all__ = [
    "ABILITIES",
    "ability_modifier",
    "proficiency_bonus",
    "level_for_xp",
    "next_level_xp",
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


@dataclass
class CharacterSheet:
    """A PC as pure, validated data.

    Derived stats (level, proficiency, modifiers) are computed properties —
    never stored, so they cannot drift from the numbers they derive from.
    """

    name: str
    hp: int = 10
    max_hp: int = 10
    ac: int = 10
    abilities: dict[str, int] = field(default_factory=lambda: {a: 10 for a in ABILITIES})
    inventory: dict[str, int] = field(default_factory=dict)
    gold: int = 0
    xp: int = 0

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
