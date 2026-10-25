"""Check resolution (roadmap M1): d20 vs DC, advantage/disadvantage, crits.

Target API (M1 implements):

    def ability_check(
        source: RandomSource,
        modifier: int,
        dc: int,
        *,
        advantage: bool = False,
        disadvantage: bool = False,
    ) -> CheckResult  # {d20, total, critical, fumble, success}

Ability modifiers come from the character sheet's derived stats
(abilities -> mod = (score - 10) // 2, proficiency bonus, save bonuses).
"""
