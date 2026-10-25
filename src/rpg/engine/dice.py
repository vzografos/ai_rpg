"""Dice engine (roadmap M1): formula parsing, injectable RandomSource, seeds.

Target API (M1 implements, unit-tested with seeded determinism):

    class RandomSource(Protocol):
        def roll(self, n: int, sides: int) -> int: ...

    def roll_dice(formula: str, source: RandomSource) -> RollResult
    # formulas: "1d20", "2d6+3", "2d20kh1" (advantage), "2d20kl1" (disadvantage)

Determinism contract: the same (formula, seed) is byte-identical across runs.
Reference: dnd-llm-game `game/dice.py` (~62 lines is enough).
"""
