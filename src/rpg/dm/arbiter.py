"""Arbiter (the turn pipeline step 1; minimal M3 surface, full M4): what is the
player's text actually?

- meta commands (/help /status /roll /world /ooc) -> the engine, no LLM
- in-game text -> intent, never outcome; wrapped as untrusted data
  (`<player_action>`) so embedded instructions — including fake system/DM
  instructions — are never followed (M4 anti-jailbreak form)
- identity enforcement (second person only) runs in the M4 backstops on the
  narration (engine/backstops.py: check_identity)

The meta surface is deliberately small in M3: the debug CLI already covers
the heavy verbs; the web UI gets the cheap ones the player actually types.
"""

from __future__ import annotations

from dataclasses import dataclass

from rpg.engine.dice import SeededSource, SystemSource, roll_dice
from rpg.engine.state import WorldState
from rpg.llm import prompts

__all__ = [
    "META_COMMANDS",
    "ArbiterDecision",
    "arbitrate",
]

META_COMMANDS = ("/help", "/status", "/roll", "/world", "/ooc")

_HELP_TEXT = (
    "Commands: /help (this), /status (your sheet + where you stand), "
    "/roll <formula> (a free dice roll, e.g. 2d20kh1+3), /world (the committed "
    "world), /ooc <text> (out-of-character — the DM answers directly)."
)


@dataclass(frozen=True)
class ArbiterDecision:
    """The arbiter's verdict: meta (handled by the engine, no LLM) or game."""

    is_meta: bool
    text: str  # the wrapped player line (game) or the command (meta)
    reply: str = ""  # the engine's answer (meta only)
    dice: list[dict] = ()  # free rolls (meta /roll only)


def _status_text(world: WorldState) -> str:
    if world.pc is None:
        return "No character yet."
    sheet = world.pc.sheet
    inventory = ", ".join(f"{qty}x {name}" for name, qty in sorted(sheet.inventory.items()))
    held = ", ".join(
        f"{item.name}" for item in world.items.values() if item.holder.startswith("pc:")
    )
    carrying = ", ".join(part for part in (inventory, held) if part) or "nothing but your wits"
    lines = [
        f"{sheet.name} — level {sheet.level}, HP {sheet.hp}/{sheet.max_hp}, AC {sheet.ac}, "
        f"gold {sheet.gold}, XP {sheet.xp}",
        f"Location: {world.pc.location or '(unlocated)'}",
        f"Carrying: {carrying}",
    ]
    if world.spine is not None:
        for line in world.spine.quest_lines():
            lines.append(f"Story: {line}" if line.startswith("Current") else line)
    return "\n".join(lines)


def arbitrate(player_text: str, world: WorldState) -> ArbiterDecision:
    """Split the player's text: meta command or in-game intent.

    Meta is a prefix match on `/`; everything else is in-game intent, wrapped
    as untrusted data (the M4 identity backstops enforce the second person on
    the *narration*, the wrap keeps the input from being instructions).
    """
    text = player_text.strip()
    if not text:
        return ArbiterDecision(is_meta=True, text="/help", reply=_HELP_TEXT)
    command = text.split(" ", 1)[0].lower()
    if command in META_COMMANDS:
        if command == "/help":
            return ArbiterDecision(is_meta=True, text=command, reply=_HELP_TEXT)
        if command == "/status":
            return ArbiterDecision(is_meta=True, text=command, reply=_status_text(world))
        if command == "/world":
            return ArbiterDecision(is_meta=True, text=command, reply=_status_text(world))
        if command == "/roll":
            formula = text.split(" ", 1)[1].strip() if " " in text else ""
            if not formula:
                return ArbiterDecision(
                    is_meta=True, text=command, reply="Usage: /roll <formula> (e.g. 2d20kh1+3)"
                )
            try:
                result = roll_dice(formula, SystemSource())
            except ValueError as exc:
                return ArbiterDecision(is_meta=True, text=command, reply=f"Invalid formula: {exc}")
            return ArbiterDecision(
                is_meta=True,
                text=command,
                reply=str(result),
                dice=[
                    {"label": f"free roll: {formula}", "formula": formula, "total": result.total}
                ],
            )
        if command == "/ooc":
            note = text.split(" ", 1)[1].strip() if " " in text else ""
            if not note:
                return ArbiterDecision(is_meta=True, text=command, reply="Usage: /ooc <text>")
            return ArbiterDecision(is_meta=True, text=command, reply="Noted. (out of character)")
    return ArbiterDecision(
        is_meta=False, text=prompts.player_action_wrap(text)
    )


def roll_source(seed: int | None) -> SeededSource | SystemSource:
    """The dice source for play: seeded when RPG_SEED is set (reproducible
    play for the acceptance runs), SystemRandom otherwise (M1 contract)."""
    return SeededSource(seed) if seed is not None else SystemSource()
