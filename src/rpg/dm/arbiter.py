"""Arbiter (the turn pipeline step 1; minimal M3 surface, full M4): what is the
player's text actually?

- meta commands (/help /status /roll /world /ooc) -> the engine, no LLM
- session commands (/new /save /load, M5) -> the engine's session module;
  the arbiter classifies and validates syntax, the pipeline runs the
  side effect (backup, swap, seed) and writes the final reply
- in-game text -> intent, never outcome; wrapped as untrusted data
  (`<player_action>`) so embedded instructions — including fake system/DM
  instructions — are never followed (M4 anti-jailbreak form)
- identity enforcement (second person only) runs in the M4 backstops on the
  narration (engine/backstops.py: check_identity)

The meta surface is deliberately small: the debug CLI already covers the
heavy verbs; the web UI gets the cheap ones the player actually types.
"""

from __future__ import annotations

from dataclasses import dataclass

from rpg.engine.dice import SeededSource, SystemSource, roll_dice
from rpg.engine.state import WorldState
from rpg.llm import prompts
from rpg.session import SAVE_NAME_RE

__all__ = [
    "META_COMMANDS",
    "ArbiterDecision",
    "arbitrate",
]

META_COMMANDS = ("/help", "/status", "/roll", "/world", "/ooc", "/save", "/load", "/new")

# Session verbs (M5): classified as meta, but the pipeline runs the side
# effect (backup / swap / seed) and writes the final reply — `command` names
# the verb and `arg` its argument (the save name).
SESSION_COMMANDS = ("/save", "/load", "/new")

_HELP_TEXT = (
    "Commands: /help (this), /status (your sheet + where you stand), "
    "/roll <formula> (a free dice roll, e.g. 2d20kh1+3), /world (the committed "
    "world), /ooc <text> (out-of-character — the DM answers directly), "
    "/save [name] (checkpoint the campaign), /load <name> (resume a checkpoint), "
    "/new (start a fresh campaign — only when none is running)."
)


@dataclass(frozen=True)
class ArbiterDecision:
    """The arbiter's verdict: meta (handled by the engine, no LLM) or game."""

    is_meta: bool
    text: str  # the wrapped player line (game) or the command (meta)
    reply: str = ""  # the engine's answer (meta only)
    dice: list[dict] = ()  # free rolls (meta /roll only)
    command: str = ""  # session verb ("save" | "load" | "new"); "" otherwise
    arg: str = ""  # the session verb's argument (the save name)


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
        if command in SESSION_COMMANDS:
            arg = text.split(" ", 1)[1].strip() if " " in text else ""
            if command == "/new":
                if arg:
                    return ArbiterDecision(
                        is_meta=True, text=command, reply="Usage: /new (no argument)"
                    )
                return ArbiterDecision(
                    is_meta=True, text=command, reply="Starting a fresh campaign…", command="new"
                )
            if not arg or not SAVE_NAME_RE.match(arg):
                usage = "Usage: /save [name]" if command == "/save" else "Usage: /load <name>"
                hint = "" if command == "/save" else " (a name is required)"
                return ArbiterDecision(is_meta=True, text=command, reply=usage + hint)
            if command == "/save":
                return ArbiterDecision(
                    is_meta=True,
                    text=command,
                    reply=f"Saving the campaign as {arg}…",
                    command="save",
                    arg=arg,
                )
            return ArbiterDecision(
                is_meta=True,
                text=command,
                reply=f"Resuming the campaign from {arg}…",
                command="load",
                arg=arg,
            )
    return ArbiterDecision(
        is_meta=False, text=prompts.player_action_wrap(text)
    )


def roll_source(seed: int | None) -> SeededSource | SystemSource:
    """The dice source for play: seeded when RPG_SEED is set (reproducible
    play for the acceptance runs), SystemRandom otherwise (M1 contract)."""
    return SeededSource(seed) if seed is not None else SystemSource()
