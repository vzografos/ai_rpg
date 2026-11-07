"""Per-turn judging (eval layer 3).

Two layers, both cheap to re-run:

1. **structural** — the M4 wall's checks re-run on the *committed* narration
   (forgery, contradiction, identity, secret leak), deterministic and
   model-free. The in-turn wall already re-asked before commit; this is the
   harness's independent second pass over what actually landed.
2. **judge model** — one strict-JSON call per turn scoring the four eval
   axes (secret leakage, dice-first misses, identity drift, beat progress).
   It sees the keeper's [NOW] block on purpose: it needs the secrets to tell
   an earned reveal from a leak. It never gates the game.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from rpg.engine.backstops import (
    check_contradiction,
    check_forgery,
    check_identity,
    check_secret_leak,
)
from rpg.engine.state import WorldState
from rpg.llm import prompts
from rpg.llm.provider import LLMProvider

__all__ = [
    "AXES",
    "ArchetypeReport",
    "TurnVerdict",
    "judge_turn",
    "spine_position",
    "structural_verdict",
]

# The four evaluation axes: the report's columns.
AXES: tuple[str, ...] = ("secret_leak", "dice_first", "identity", "beat_progress")


@dataclass
class TurnVerdict:
    """One judged turn: what was said, what landed, what the judge found."""

    turn: int
    player_line: str
    narration: str
    # Structural findings: backstop kind -> the details ({} when clean).
    structural: dict[str, list[str]] = field(default_factory=dict)
    # The spine before/after the turn ({} when the world has no spine).
    beat_before: dict[str, Any] = field(default_factory=dict)
    beat_after: dict[str, Any] = field(default_factory=dict)
    # The attacker's claims (saboteur) that reached the committed narration.
    claims_landed: tuple[str, ...] = ()
    # The judge model's strict-JSON verdict ({} when it failed or was off).
    verdict: dict[str, Any] = field(default_factory=dict)
    # Why the judge call failed, when it did ("" = it did not fail). The
    # report must be able to say *why* a turn shows no verdict — a silent
    # {} reads like "the judge said nothing", which is a lie about method.
    judge_error: str = ""
    # Pipeline errors recorded this turn (re-asks, provider failures, ...).
    errors: tuple[str, ...] = ()

    @property
    def clean(self) -> bool:
        return not self.structural

    @property
    def progressed(self) -> bool:
        return bool(self.beat_before) and self.beat_after != self.beat_before

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ArchetypeReport:
    """One archetype run: the per-turn verdicts and the end state."""

    archetype: str
    intent: str
    target_beat: str | None
    turns: list[TurnVerdict] = field(default_factory=list)
    final_position: dict[str, Any] = field(default_factory=dict)
    is_over: bool = False
    errors: list[str] = field(default_factory=list)
    # Aggregate usage across the run (prompt/completion tokens, model calls).
    usage: dict[str, int] = field(default_factory=dict)

    @property
    def clean_turns(self) -> int:
        return sum(1 for turn in self.turns if turn.clean)

    @property
    def violations(self) -> dict[str, list[str]]:
        merged: dict[str, list[str]] = {}
        for turn in self.turns:
            for kind, details in turn.structural.items():
                merged.setdefault(kind, []).extend(
                    f"turn {turn.turn}: {detail}" for detail in details
                )
        return merged

    @property
    def claims_landed(self) -> list[str]:
        landed: list[str] = []
        for turn in self.turns:
            landed.extend(f"turn {turn.turn}: {claim}" for claim in turn.claims_landed)
        return landed

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def spine_position(world: WorldState) -> dict[str, Any]:
    """The judge's spine snapshot: how far the story has advanced."""
    spine = world.spine
    if spine is None:
        return {}
    active = spine.active_beat
    return {
        "completed": sum(1 for beat in spine.beats if beat.status == "done"),
        "active": active.text if active is not None else None,
        "is_over": spine.is_over,
    }


def structural_verdict(
    narration: str,
    world: WorldState,
    *,
    dice: list[dict[str, Any]],
    authoritative: list[str],
) -> dict[str, list[str]]:
    """The M4 wall re-run on the committed narration ({} when clean)."""
    pc_name = world.pc.sheet.name if world.pc is not None else ""
    checks = {
        "forgery": check_forgery(narration, dice, authoritative, world),
        "contradiction": check_contradiction(narration, dice, authoritative, world),
        "identity": check_identity(narration, pc_name),
        "secret_leak": check_secret_leak(narration, world),
    }
    return {
        kind: [violation.detail for violation in violations]
        for kind, violations in checks.items()
        if violations
    }


async def judge_turn(
    provider: LLMProvider,
    *,
    player_line: str,
    narration: str,
    world: WorldState,
    committed_lines: list[str],
) -> dict[str, Any]:
    """The judge model's strict-JSON verdict for one turn.

    Propagates on failure (bad JSON, transport error): the caller records the
    reason on the turn. A judge failure never loses the turn or the run — the
    structural layer still scored it.
    """
    spine = world.spine
    now_block = spine.now_block() if spine is not None else ""
    user = prompts.judge_user(player_line, narration, now_block, committed_lines)
    verdict = await provider.extract_json(prompts.JUDGE_SYSTEM, user)
    allowed = {axis: verdict.get(axis) for axis in AXES}
    notes = str(verdict.get("notes", ""))
    return {**allowed, "notes": notes}
