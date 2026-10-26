"""Dice engine (roadmap M1): formula grammar, keep-highest/lowest, sources."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from rpg.engine.dice import (
    SeededSource,
    SystemSource,
    parse_formula,
    roll_dice,
)


class CannedSource:
    """Returns pre-canned dice one at a time (the parser rolls die by die)."""

    def __init__(self, values: list[int]) -> None:
        self.values = list(values)
        self.calls: list[tuple[int, int]] = []

    def roll(self, n: int, sides: int) -> int:
        assert n == 1
        self.calls.append((n, sides))
        return self.values.pop(0)


# -- grammar ---------------------------------------------------------------


def test_parse_formula_plain() -> None:
    formula = parse_formula("1d20")
    assert (formula.n, formula.sides, formula.keep, formula.keep_count, formula.modifier) == (
        1,
        20,
        None,
        None,
        0,
    )


def test_parse_formula_modifiers() -> None:
    assert parse_formula("2d6+3").modifier == 3
    assert parse_formula("1d20-5").modifier == -5
    assert parse_formula("4d6").n == 4


def test_parse_formula_keep_highest_and_lowest() -> None:
    assert parse_formula("2d20kh1").keep == "h"
    assert parse_formula("2d20kh1").keep_count == 1
    assert parse_formula("2d20kl1").keep == "l"
    assert parse_formula("4d6kh2").keep_count == 2


def test_parse_formula_keep_then_modifier() -> None:
    formula = parse_formula("2d20kh1+4")
    assert (formula.keep, formula.keep_count, formula.modifier) == ("h", 1, 4)
    formula = parse_formula("2d20kl1-2")
    assert (formula.keep, formula.keep_count, formula.modifier) == ("l", 1, -2)


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "d20",
        "1d",
        "1d0",
        "0d6",
        "1d-5",
        "1d20k",
        "2d20k0",
        "2d20k3",
        "2d20kh3",
        "1d20kl2",
        "1d20x",
        "1d20+3+4",
        " 1d20",
        "2d20kh",
        "2d20kz1",
    ],
)
def test_parse_formula_rejects_invalid(bad: str) -> None:
    with pytest.raises(ValueError, match="invalid dice formula"):
        parse_formula(bad)


# -- rolling ---------------------------------------------------------------


def test_roll_dice_single_die() -> None:
    result = roll_dice("1d20", CannedSource([7]))
    assert (result.rolled, result.kept, result.modifier, result.total) == ((7,), (7,), 0, 7)


def test_roll_dice_sums_dices_and_adds_modifier() -> None:
    result = roll_dice("2d6+3", CannedSource([2, 5]))
    assert result.rolled == (2, 5)
    assert result.kept == (2, 5)
    assert result.total == 10


def test_roll_dice_negative_modifier() -> None:
    assert roll_dice("1d20-5", CannedSource([3])).total == -2


def test_roll_dice_keep_highest() -> None:
    result = roll_dice("2d20kh1", CannedSource([3, 17]))
    assert result.kept == (17,)
    assert result.total == 17


def test_roll_dice_keep_lowest() -> None:
    result = roll_dice("2d20kl1", CannedSource([3, 17]))
    assert result.kept == (3,)
    assert result.total == 3


def test_roll_dice_keep_count_greater_than_one() -> None:
    result = roll_dice("4d6kh2", CannedSource([6, 1, 3, 5]))
    assert result.kept == (6, 5)
    assert result.total == 11


def test_roll_dice_keep_all() -> None:
    result = roll_dice("2d6kh2", CannedSource([2, 5]))
    assert result.kept == (5, 2)
    assert result.total == 7


def test_roll_dice_asks_for_one_die_at_a_time() -> None:
    source = CannedSource([4, 9])
    roll_dice("2d6", source)
    assert source.calls == [(1, 6), (1, 6)]


def test_roll_result_is_frozen() -> None:
    result = roll_dice("1d20", CannedSource([7]))
    with pytest.raises(FrozenInstanceError):
        result.total = 5


def test_roll_result_str_is_canonical() -> None:
    expected = "2d20kh1 rolled=[3, 17] kept=[17] total=17"
    assert str(roll_dice("2d20kh1", CannedSource([3, 17]))) == expected
    expected = "2d6+3 rolled=[2, 5] kept=[2, 5] total=10"
    assert str(roll_dice("2d6+3", CannedSource([2, 5]))) == expected


# -- sources ---------------------------------------------------------------


def test_seeded_source_roll_bounds() -> None:
    source = SeededSource(123)
    for _ in range(50):
        assert 3 <= source.roll(3, 6) <= 18
    assert 1 <= source.roll(1, 20) <= 20


def test_seeded_source_rejects_bad_dice() -> None:
    source = SeededSource(0)
    with pytest.raises(ValueError, match="at least 1 die"):
        source.roll(0, 6)
    with pytest.raises(ValueError, match="at least 1 side"):
        source.roll(2, 0)


def test_system_source_roll_bounds() -> None:
    source = SystemSource()
    assert 1 <= source.roll(1, 20) <= 20
    assert 2 <= source.roll(2, 6) <= 12


def test_system_source_rejects_bad_dice() -> None:
    with pytest.raises(ValueError, match="at least 1 die"):
        SystemSource().roll(0, 6)
    with pytest.raises(ValueError, match="at least 1 side"):
        SystemSource().roll(2, 0)
