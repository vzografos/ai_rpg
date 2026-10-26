"""Dice engine (roadmap M1): formula parsing, injectable RandomSource, seeds.

Grammar (strict, one order): `NdS` + optional `khK`/`klK` (keep the
highest/lowest K of the N dice) + optional `+M`/`-M` flat modifier — e.g.
"1d20", "2d6+3", "2d20kh1" (advantage), "2d20kl1" (disadvantage),
"2d20kh1+4". Anything else is a ValueError.

Determinism contract: the same (formula, seed) is byte-identical across runs.
Reference: dnd-llm-game `game/dice.py` (~62 lines is enough).
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from typing import Protocol

__all__ = [
    "Formula",
    "parse_formula",
    "RandomSource",
    "SeededSource",
    "SystemSource",
    "RollResult",
    "roll_dice",
]

_FORMULA_RE = re.compile(r"^(\d+)d(\d+)(?:k([hl])(\d+))?(?:([+-])(\d+))?$")


@dataclass(frozen=True)
class Formula:
    """A parsed dice formula: `n`d`sides`, optionally keeping the
    highest/lowest `keep_count` dice, plus a flat `modifier`."""

    n: int
    sides: int
    keep: str | None  # "h" | "l" | None
    keep_count: int | None
    modifier: int


def parse_formula(formula: str) -> Formula:
    """Parse and validate a dice formula (grammar in the module docstring)."""
    match = _FORMULA_RE.match(formula)
    if match is None:
        raise ValueError(f"invalid dice formula: {formula!r}")
    n, sides, keep, keep_count, sign, modifier = match.groups()
    n, sides = int(n), int(sides)
    modifier = int(f"{sign}{modifier}") if sign else 0
    keep_count = int(keep_count) if keep_count is not None else None
    if n < 1:
        raise ValueError(f"invalid dice formula (need at least 1 die): {formula!r}")
    if sides < 1:
        raise ValueError(f"invalid dice formula (need at least 1 side): {formula!r}")
    if keep_count is not None and not 1 <= keep_count <= n:
        raise ValueError(f"invalid dice formula (keep out of range): {formula!r}")
    return Formula(n=n, sides=sides, keep=keep, keep_count=keep_count, modifier=modifier)


class RandomSource(Protocol):
    """Source of dice rolls.

    `roll(n, sides)` returns the sum of `n` independent rolls of a
    `sides`-sided die (each in 1..sides). Inject this into the engine to
    make every call deterministic — the M1 core is zero I/O by design.
    """

    def roll(self, n: int, sides: int) -> int: ...


@dataclass(frozen=True)
class RollResult:
    """A resolved roll.

    `rolled` holds every die in roll order; `kept` the dice counted after
    any kh/kl selection (equal to `rolled` when the formula keeps none);
    `total` = sum(kept) + modifier. `str()` is the canonical form —
    byte-identical for the same (formula, seed), per the M1 acceptance.
    """

    formula: str
    rolled: tuple[int, ...]
    kept: tuple[int, ...]
    modifier: int
    total: int

    def __str__(self) -> str:
        rolled = ", ".join(str(die) for die in self.rolled)
        kept = ", ".join(str(die) for die in self.kept)
        return f"{self.formula} rolled=[{rolled}] kept=[{kept}] total={self.total}"


def _check_dice(n: int, sides: int) -> None:
    if n < 1:
        raise ValueError(f"need at least 1 die, got {n}")
    if sides < 1:
        raise ValueError(f"need at least 1 side, got {sides}")


def roll_dice(formula: str, source: RandomSource) -> RollResult:
    """Parse `formula` and roll it against `source`."""
    parsed = parse_formula(formula)
    _check_dice(parsed.n, parsed.sides)
    rolled = tuple(source.roll(1, parsed.sides) for _ in range(parsed.n))
    if parsed.keep_count is None:
        kept = rolled
    elif parsed.keep == "h":
        kept = tuple(sorted(rolled, reverse=True)[: parsed.keep_count])
    else:
        kept = tuple(sorted(rolled)[: parsed.keep_count])
    return RollResult(
        formula=formula,
        rolled=rolled,
        kept=kept,
        modifier=parsed.modifier,
        total=sum(kept) + parsed.modifier,
    )


class SeededSource:
    """Deterministic RandomSource: `random.Random(seed)`.

    The same seed always yields the same roll sequence — the M1 acceptance
    ("`roll` output byte-identical under the same seed") rides on this.
    """

    def __init__(self, seed: int) -> None:
        self._rng = random.Random(seed)

    def roll(self, n: int, sides: int) -> int:
        _check_dice(n, sides)
        return sum(self._rng.randint(1, sides) for _ in range(n))


class SystemSource:
    """Non-deterministic RandomSource for actual play (SystemRandom)."""

    def __init__(self) -> None:
        self._rng = random.SystemRandom()

    def roll(self, n: int, sides: int) -> int:
        _check_dice(n, sides)
        return sum(self._rng.randint(1, sides) for _ in range(n))
