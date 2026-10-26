"""Check resolution (roadmap M1): d20 vs DC, advantage/disadvantage, crits.

    ability_check(source, modifier, dc, *, advantage=..., disadvantage=...)
        -> CheckResult {d20, modifier, dc, total, critical, fumble, success}

    ability_save(source, sheet, ability, dc, *, ...)
        -> CheckResult   # modifier = the sheet's save bonus

Rules (SRD 5.1 subset): roll 1d20 — or 2d20kh1 on advantage, 2d20kl1 on
disadvantage (both together cancel) — add the modifier, compare to the DC.
A natural 20 always succeeds (critical); a natural 1 always fails (fumble).
"""

from __future__ import annotations

from dataclasses import dataclass

from rpg.engine.dice import RandomSource, roll_dice
from rpg.engine.sheet import CharacterSheet

__all__ = ["CheckResult", "ability_check", "ability_save"]


@dataclass(frozen=True)
class CheckResult:
    d20: int
    modifier: int
    dc: int
    total: int
    critical: bool
    fumble: bool
    success: bool


def _roll_d20(source: RandomSource, *, advantage: bool, disadvantage: bool) -> int:
    if advantage and disadvantage:
        formula = "1d20"
    elif advantage:
        formula = "2d20kh1"
    elif disadvantage:
        formula = "2d20kl1"
    else:
        formula = "1d20"
    return roll_dice(formula, source).kept[0]


def ability_check(
    source: RandomSource,
    modifier: int,
    dc: int,
    *,
    advantage: bool = False,
    disadvantage: bool = False,
) -> CheckResult:
    """A d20 + modifier against a DC (the modifier may be negative)."""
    if dc < 1:
        raise ValueError(f"DC must be >= 1: {dc}")
    d20 = _roll_d20(source, advantage=advantage, disadvantage=disadvantage)
    total = d20 + modifier
    critical = d20 == 20
    fumble = d20 == 1
    success = critical or (not fumble and total >= dc)
    return CheckResult(
        d20=d20,
        modifier=modifier,
        dc=dc,
        total=total,
        critical=critical,
        fumble=fumble,
        success=success,
    )


def ability_save(
    source: RandomSource,
    sheet: CharacterSheet,
    ability: str,
    dc: int,
    *,
    advantage: bool = False,
    disadvantage: bool = False,
) -> CheckResult:
    """An ability save: ability_check at the sheet's save bonus
    (ability modifier + proficiency)."""
    return ability_check(
        source,
        sheet.save_bonus(ability),
        dc,
        advantage=advantage,
        disadvantage=disadvantage,
    )
