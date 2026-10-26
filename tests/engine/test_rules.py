"""Check resolution (roadmap M1): d20 vs DC, advantage/disadvantage, crits."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from rpg.engine.rules import CheckResult, ability_check, ability_save
from rpg.engine.sheet import CharacterSheet


class CannedSource:
    """Pre-canned dice, one at a time (the parser rolls die by die)."""

    def __init__(self, values: list[int]) -> None:
        self.values = list(values)

    def roll(self, n: int, sides: int) -> int:
        assert n == 1
        return self.values.pop(0)


def test_ability_check_success_at_the_boundary() -> None:
    result = ability_check(CannedSource([12]), 3, 15)
    assert result == CheckResult(
        d20=12, modifier=3, dc=15, total=15, critical=False, fumble=False, success=True
    )


def test_ability_check_failure_below_dc() -> None:
    assert ability_check(CannedSource([11]), 3, 15).success is False


def test_ability_check_negative_modifier() -> None:
    result = ability_check(CannedSource([12]), -1, 11)
    assert (result.total, result.success) == (11, True)


def test_ability_check_advantage_keeps_highest() -> None:
    result = ability_check(CannedSource([3, 17]), 0, 15, advantage=True)
    assert (result.d20, result.success) == (17, True)


def test_ability_check_disadvantage_keeps_lowest() -> None:
    result = ability_check(CannedSource([3, 17]), 0, 5, disadvantage=True)
    assert (result.d20, result.success) == (3, False)


def test_ability_check_advantage_and_disadvantage_cancel() -> None:
    result = ability_check(CannedSource([9]), 0, 10, advantage=True, disadvantage=True)
    assert (result.d20, result.success) == (9, False)  # only one die was rolled


def test_ability_check_natural_20_is_always_a_critical_success() -> None:
    result = ability_check(CannedSource([20]), -5, 20)
    assert (result.critical, result.fumble, result.success) == (True, False, True)


def test_ability_check_natural_1_is_always_a_fumble_failure() -> None:
    result = ability_check(CannedSource([1]), 5, 1)
    assert (result.critical, result.fumble, result.success) == (False, True, False)


def test_ability_check_rejects_bad_dc() -> None:
    with pytest.raises(ValueError, match="DC must be >= 1"):
        ability_check(CannedSource([10]), 0, 0)


def test_ability_save_uses_the_sheet_save_bonus() -> None:
    sheet = CharacterSheet(name="Bran", xp=2700)  # level 4 -> proficiency 1
    sheet.abilities["wisdom"] = 14  # modifier +2
    result = ability_save(CannedSource([11]), sheet, "wisdom", 13)
    assert (result.modifier, result.total, result.success) == (3, 14, True)


def test_ability_save_propagates_condition_flags() -> None:
    sheet = CharacterSheet(name="Bran")
    result = ability_save(CannedSource([3, 17]), sheet, "strength", 20, disadvantage=True)
    assert result.d20 == 3  # the low die is kept
    assert result.success is False


def test_check_result_is_frozen() -> None:
    result = ability_check(CannedSource([12]), 3, 15)
    with pytest.raises(FrozenInstanceError):
        result.total = 1
