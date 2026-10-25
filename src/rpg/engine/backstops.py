"""Structural backstops (roadmap M4): the wall, not the behaviour.

Checks run on the final narration WITHOUT reading the fiction:
- forgery         roll-shaped number in prose but no dice tool ran this turn
- contradiction   prose number disagrees with the real roll
- identity        protagonist referred to in third person (solo drift)
- combat transition prose pre-resolves hit/miss/damage/condition or invents
                  an unregistered attacker

Violation -> one Stop-form re-ask, bounded (<=3), then a deterministic
fallback narration rendered from committed engine state. The red-line suite
(M4) proves each check pins a failure: remove a backstop -> a test goes red.
"""
