"""Structural backstops (roadmap M4): the wall, not the behaviour.

Checks run on the final narration WITHOUT reading the fiction — they match
claim-shaped patterns in the prose and compare only against committed
engine data (the turn's dice frames, the authoritative event lines, the
turn's events, the world's numbers, the keeper-only secrets):

- forgery         a roll-shaped number in prose, no dice tool ran this
                  turn, and no committed event carries the number
- contradiction   a roll-shaped number in prose disagrees with the real
                  rolls / committed events of the turn
- identity        protagonist referred to in third person (solo drift)
- combat transition prose pre-resolves a hit/damage/condition the engine
                  never committed, or names an unregistered attacker
- lost narration  the model returned nothing (the turn still commits)
- secret leak     the narration quotes keeper-only material verbatim

Violation -> one Stop-form re-ask, re-verified after each re-ask, bounded
(<= MAX_BACKSTOP_REASKS, in dm/loop.py); then a deterministic fallback
narration rendered from the committed authoritative lines. The red-line
suite (tests/redline) proves each check pins a failure: a backstop
disabled -> its test goes red.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from rpg.engine.state import WorldState

__all__ = [
    "MAX_BACKSTOP_REASKS",
    "BACKSTOP_KINDS",
    "Violation",
    "claim_numbers",
    "allowed_numbers",
    "check_forgery",
    "check_contradiction",
    "check_identity",
    "check_lost_narration",
    "check_combat_transition",
    "check_secret_leak",
    "keeper_secrets",
    "run_backstops",
    "fallback_narration",
]

MAX_BACKSTOP_REASKS = 3  # bounded Stop-form correction loop (the bounded correction loop)

BACKSTOP_KINDS = (
    "forgery",
    "contradiction",
    "identity",
    "lost_narration",
    "combat_transition",
    "secret_leak",
)


@dataclass(frozen=True)
class Violation:
    """One structural finding: the kind names the backstop, the detail the
    reason the Stop-form re-ask shows the model."""

    kind: str
    detail: str


# --- roll-shaped claims (the numbers a narration is only allowed to state) ---
#
# A claim is a number wearing the shape of a rule result: a d20, a DC
# comparison, a to-hit, damage, an XP or gold payout. Anything else (counts
# of objects, HP totals, dates) is ordinary prose and stays out of scope —
# the checks trade a little recall for zero false positives on honest play.

_N = r"(\d+)"

_CLAIM_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(rf"\broll(?:ed|s)?\s+(?:a |the |an )?{_N}", re.IGNORECASE),
    re.compile(rf"\b{_N}\s+(?:vs|against)\s+DC\s+{_N}", re.IGNORECASE),
    re.compile(rf"\bto-hit\s+(?:roll\s+of\s+)?{_N}", re.IGNORECASE),
    re.compile(rf"\b{_N}\s+damage\b", re.IGNORECASE),
    re.compile(
        rf"\bhit(?:s|ting)?\s+(?:you|him|her|them|it|the [\w-]+)\s+for\s+{_N}", re.IGNORECASE
    ),
    re.compile(
        rf"\bdeal(?:s|ing)?[^.,;]{{0,24}}?{_N}(?:\s+points\s+of)?\s+damage\b", re.IGNORECASE
    ),
    re.compile(rf"\btak(?:e|es|en)\s+{_N}(?:\s+points\s+of)?\s+damage\b", re.IGNORECASE),
    re.compile(rf"\bgain(?:s|ed)?\s+(?:a |an )?{_N}\s+XP\b", re.IGNORECASE),
    re.compile(rf"\b{_N}(?:\s+pieces\s+of)?\s+gold\b", re.IGNORECASE),
)


def claim_numbers(narration: str) -> list[int]:
    """Every roll-shaped number the narration states (deduped, in order).

    A claim pattern may carry more than one number (the DC in "N vs DC M"
    is claimed too) — every numeric group counts."""
    found: list[int] = []
    for pattern in _CLAIM_PATTERNS:
        for match in pattern.finditer(narration):
            for group in match.groups():
                if group is not None:
                    number = int(group)
                    if number not in found:
                        found.append(number)
    return found


def _state_numbers(world: WorldState) -> set[int]:
    """Numbers the engine itself currently displays (state, sheet, combat)."""
    numbers: set[int] = set()
    pc = world.pc
    if pc is not None:
        sheet = pc.sheet
        numbers.update(
            (sheet.hp, sheet.max_hp, sheet.gold, sheet.xp, sheet.level, sheet.ac, sheet.proficiency)
        )
        numbers.update(sheet.inventory.values())
    for item in world.items.values():
        numbers.add(item.quantity)
    if world.combat is not None:
        for record in world.combat.participants.values():
            numbers.update((record.hp, record.max_hp, record.ac, record.attack_bonus))
    return numbers


def allowed_numbers(
    dice: list[dict[str, Any]], authoritative: list[str], world: WorldState
) -> set[int]:
    """Every number the engine produced this turn (or currently displays).

    The dice frames carry the roll values; the authoritative lines carry
    the engine's own wording of every number; the state numbers are what
    the engine-provided state block already showed the player. Die names
    ("d20", "d6") are stripped first: their digits are not claimed values.
    """
    allowed: set[int] = set()
    for frame in dice:
        for key in ("total", "d20", "dc", "ac"):
            value = frame.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                allowed.add(value)
    for line in authoritative:
        without_die_names = re.sub(r"\bd\d+\b", " ", line)
        allowed.update(int(match) for match in re.findall(r"\d+", without_die_names))
    allowed.update(_state_numbers(world))
    return allowed


# --- the checks ---------------------------------------------------------------


def check_forgery(
    narration: str,
    dice: list[dict[str, Any]],
    authoritative: list[str],
    world: WorldState,
) -> list[Violation]:
    """A roll-shaped number in prose, but no dice tool ran this turn and no
    committed event carries the number (the model invented the result)."""
    if not narration.strip():
        return []
    if dice:
        return []  # numbers next to real rolls are contradiction's job
    allowed = allowed_numbers(dice, authoritative, world)
    violations = []
    for number in claim_numbers(narration):
        if number not in allowed:
            violations.append(
                Violation(
                    "forgery",
                    f"the narration states {number} but no dice tool ran this turn "
                    "and no committed event carries that number",
                )
            )
    return violations


def check_contradiction(
    narration: str,
    dice: list[dict[str, Any]],
    authoritative: list[str],
    world: WorldState,
) -> list[Violation]:
    """A roll-shaped number in prose disagrees with the real rolls of the
    turn (the model narrated a result the engine did not commit)."""
    if not narration.strip() or not dice:
        return []
    allowed = allowed_numbers(dice, authoritative, world)
    violations = []
    for number in claim_numbers(narration):
        if number not in allowed:
            violations.append(
                Violation(
                    "contradiction",
                    f"the narration states {number} but this turn's rolls and "
                    "committed events do not carry that number",
                )
            )
    return violations


_QUOTED = re.compile(r'"[^"]*"')


def check_identity(narration: str, pc_name: str) -> list[Violation]:
    """The protagonist referred to in third person (solo drift): the
    narration addresses the player as 'you', never by name. A name inside
    quoted speech is dialogue, not narration."""
    if not narration.strip() or not pc_name:
        return []
    text = _QUOTED.sub("", narration)
    names = [pc_name]
    first = pc_name.split(" ", 1)[0]
    if first and first != pc_name:
        names.append(first)
    violations = []
    for name in dict.fromkeys(names):
        if re.search(rf"\b{re.escape(name)}\b", text, re.IGNORECASE):
            violations.append(
                Violation(
                    "identity",
                    f"the protagonist is referred to in third person ({name!r}); "
                    "narrate in the second person ('you')",
                )
            )
    return violations


def check_lost_narration(narration: str) -> list[Violation]:
    """The model returned nothing (the turn is never lost: it commits the
    deterministic fallback instead of an empty line)."""
    if not narration.strip():
        return [Violation("lost_narration", "the narration is empty")]
    return []


# --- combat transition ---------------------------------------------------------

_PRONOUNS = frozenset(("it", "he", "she", "they", "who", "what", "one"))

_CONDITION_WORDS = (
    "stunned",
    "paralyzed",
    "grappled",
    "knocked unconscious",
    "knocked down",
)

# Case-insensitive: a claim at the start of a sentence capitalizes "You".
_COMBAT_CLAIM_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "attacker",
        re.compile(
            r"\b([A-Z][\w' -]{0,24}?)\s+"
            r"(?:hits|strikes|cuts|slashes|bites|claws|grabs|seizes|smashes)\s+you\b",
            re.IGNORECASE,
        ),
    ),
    (
        "damage_on_you",
        re.compile(
            r"\b(?:hits|strikes|cuts|slashes|smashes)\s+you\s+for\s+(\d+)\b", re.IGNORECASE
        ),
    ),
    (
        "attacker",
        re.compile(
            r"\b([A-Z][\w' -]{0,24}?)\s+deal(?:s|ing)?\s+(\d+)\s+(?:points of )?damage\b",
            re.IGNORECASE,
        ),
    ),
    (
        "damage_on_you",
        re.compile(
            r"\byou\s+(?:take|suffer|receive)\s+(\d+)\s+(?:points of )?damage\b", re.IGNORECASE
        ),
    ),
    (
        "pc_attack",
        re.compile(
            r"\byou\s+(?:hit|strike|cut|slash)\s+([\w' -]{2,26}?)\s+for\s+(\d+)\b", re.IGNORECASE
        ),
    ),
    ("condition", re.compile(rf"\byou\s+are\s+({'|'.join(_CONDITION_WORDS)})\b", re.IGNORECASE)),
    ("condition", re.compile(r"\b(stuns|paralyzes|seizes)\s+you\b", re.IGNORECASE)),
    (
        "condition",
        re.compile(
            r"\b(?:stuns|paralyzes|seizes|knocks)\s+you\s+(down|unconscious)\b", re.IGNORECASE
        ),
    ),
)

_CONDITION_MAP = {
    "stunned": "stunned",
    "paralyzed": "stunned",
    "stuns": "stunned",
    "paralyzes": "stunned",
    "seizes": "stunned",
    "grappled": None,
    "knocked unconscious": "unconscious",
    "unconscious": "unconscious",
    "knocked down": "prone",
    "down": "prone",
    "knocks": "prone",
}


def _normalize_name(name: str) -> str:
    norm = " ".join(name.casefold().split())
    if norm.startswith("the "):
        norm = norm[4:]
    return norm


def _name_matches(name: str, participants: set[str]) -> bool:
    for participant in participants:
        pnorm = _normalize_name(participant)
        if pnorm and (name == pnorm or name in pnorm or pnorm in name):
            return True
    return False


def _combat_claims(narration: str) -> list[tuple[str, str | None, str]]:
    """The combat-result claims in the prose: (role, subject, text).

    Roles: 'attacker' (a creature strikes the player), 'pc_attack' (the
    player strikes a named target), 'damage_on_you' (damage on the player),
    'condition' (a condition applied to the player). Deduped by text.
    """
    claims: list[tuple[str, str | None, str]] = []
    seen: set[str] = set()
    for role, pattern in _COMBAT_CLAIM_PATTERNS:
        for match in pattern.finditer(narration):
            text = match.group(0)
            if text in seen:
                continue
            seen.add(text)
            if role == "attacker":
                claims.append((role, match.group(1), text))
            elif role == "pc_attack":
                claims.append((role, match.group(1), text))
            elif role == "condition":
                groups = match.groups()
                claims.append((role, groups[-1], text))
            else:
                claims.append((role, None, text))
    return claims


def check_combat_transition(narration: str, world: WorldState, events: list[tuple[str, Any]]) -> list[Violation]:  # noqa: E501
    """Prose that pre-resolves combat the engine never committed, or that
    invents an attacker the encounter does not contain.

    The engine resolves every encounter end to end and commits each attack
    (combat_attack) — a narration may only reflect those committed results.
    """
    if not narration.strip():
        return []
    attacks = [payload for kind, payload in events if kind == "combat_attack"]
    participants: set[str] = set()
    if world.combat is not None:
        participants = set(world.combat.participants)
    for kind, payload in events:
        if kind == "combat_begin":
            participants.update(p["name"] for p in payload.get("participants", []))
        elif kind == "combat_attack":
            if payload.get("attacker"):
                participants.add(payload["attacker"])
            if payload.get("defender"):
                participants.add(payload["defender"])
    committed_conditions: set[str] = set()
    for kind, payload in events:
        if kind == "combat_condition" and payload.get("add"):
            condition = payload.get("condition")
            if condition:
                committed_conditions.add(condition)
    pc_name = world.pc.sheet.name if world.pc is not None else ""
    if world.combat is not None and pc_name in world.combat.participants:
        committed_conditions.update(world.combat.participants[pc_name].conditions)

    claims = _combat_claims(narration)
    if not claims:
        return []
    violations: list[Violation] = []
    if not attacks and not participants:
        for _role, _subject, text in claims:
            violations.append(
                Violation(
                    "combat_transition",
                    f"the narration pre-resolves a combat result the engine never "
                    f"committed: {text!r}",
                )
            )
        return violations

    for role, subject, _text in claims:
        if role in ("attacker", "pc_attack") and subject is not None:
            subject_norm = _normalize_name(subject)
            if subject_norm in _PRONOUNS:
                continue
            if not _name_matches(subject_norm, participants):
                label = "attacker" if role == "attacker" else "combatant"
                violations.append(
                    Violation(
                        "combat_transition",
                        f"the narration invents an unregistered {label}: "
                        f"{subject!r} (participants: {', '.join(sorted(participants))})",
                    )
                )
        elif role == "condition" and subject is not None:
            canonical = _CONDITION_MAP.get(subject)
            if canonical is None:
                violations.append(
                    Violation(
                        "combat_transition",
                        f"the narration applies a condition the engine does not "
                        f"track: {subject!r}",
                    )
                )
            elif canonical not in committed_conditions:
                violations.append(
                    Violation(
                        "combat_transition",
                        f"the narration applies {canonical!r} to the player but no "
                        "committed event carries it",
                    )
                )
    return violations


# --- secrets --------------------------------------------------------------------

def keeper_secrets(world: WorldState) -> list[str]:
    """The keeper-only strings: what narration must never quote verbatim.

    NPC agendas (world record + spine cast), the spine's premise/stakes/
    antagonist/finale, beat details and planned-event details. The
    player-safe projection (quest lines) never contains any of these.
    """
    secrets: list[str] = []
    for npc in world.npcs.values():
        if npc.agenda:
            secrets.append(npc.agenda)
    spine = world.spine
    if spine is not None:
        for part in (spine.premise, spine.stakes, spine.antagonist, spine.finale):
            if part:
                secrets.append(part)
        for beat in spine.beats:
            if beat.detail:
                secrets.append(beat.detail)
        for member in spine.cast:
            if member.agenda:
                secrets.append(member.agenda)
        for event in spine.events:
            if event.detail:
                secrets.append(event.detail)
    return secrets


def check_secret_leak(narration: str, world: WorldState) -> list[Violation]:
    """The narration quotes keeper-only material verbatim (a secret probe
    answered with the secret). Paraphrase is allowed; verbatim is not."""
    if not narration.strip():
        return []
    lowered = narration.casefold()
    violations = []
    for secret in dict.fromkeys(secret for secret in keeper_secrets(world) if len(secret) >= 12):
        if secret.casefold() in lowered:
            violations.append(
                Violation("secret_leak", "the narration quotes keeper-only material verbatim")
            )
    return violations


# --- the orchestrator -------------------------------------------------------------


def run_backstops(
    narration: str,
    world: WorldState,
    *,
    dice: list[dict[str, Any]],
    events: list[tuple[str, Any]],
    authoritative: list[str],
    kinds: tuple[str, ...] = BACKSTOP_KINDS,
) -> list[Violation]:
    """Run the structural checks the wall is made of (step 6, the turn pipeline).

    `kinds` lets the red-line suite disable one backstop at a time to prove
    each one earns its keep (a disabled backstop -> its test goes red).
    """
    violations: list[Violation] = []
    if "lost_narration" in kinds:
        violations += check_lost_narration(narration)
    if "forgery" in kinds:
        violations += check_forgery(narration, dice, authoritative, world)
    if "contradiction" in kinds:
        violations += check_contradiction(narration, dice, authoritative, world)
    if "identity" in kinds:
        name = world.pc.sheet.name if world.pc is not None else ""
        violations += check_identity(narration, name)
    if "combat_transition" in kinds:
        violations += check_combat_transition(narration, world, events)
    if "secret_leak" in kinds:
        violations += check_secret_leak(narration, world)
    return violations


def fallback_narration(authoritative: list[str]) -> str:
    """The deterministic narration rendered from committed state (M4): the
    engine's own wording of the turn's committed events, verbatim, plus the
    open choice. A fully compromised model can only narrate what the
    engine already committed."""
    if not authoritative:
        return "The moment hangs, undecided. What do you do?"
    return " ".join(authoritative[-3:]) + " What do you do?"
