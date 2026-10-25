"""Combat subset (roadmap M1): initiative, attack resolution, HP, conditions.

Target scope (M1 implements):
- initiative order (d20 + Dex mod, ties by name)
- attack resolution: to-hit d20 + mod vs AC, damage dice, crits (2x dice)
- conditions: poisoned, stunned, prone, unconscious
- death saves; death at 0 HP

Pure and deterministic given a RandomSource — no LLM, no I/O.
"""
