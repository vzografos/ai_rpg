"""Context builder (roadmap M2): stable head + volatile tail + trimmed history.

Layout (Loreweaver's split, the state/memory layout):
- stable head (system message, cacheable): DM identity, rules summary,
  GM-quality standards, keeper-discipline block, spine [NOW] block (M3),
  rolling campaign summary (M5).
- volatile tail (user message, headed "Current game state
  (engine-provided, not spoken by any player)"): location, PC sheet, NPC
  roster (agendas never — keeper-only), open quests, items, recalled facts,
  current combat, trimmed recent transcript, player line.

The prompt is metered (token heuristic: chars/4) and the transcript trims
toward the budget. `dump_context` renders the whole thing for the
`/dump-context` debug command (CLI + web).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from rpg.engine.events import Message
from rpg.engine.state import WorldState
from rpg.llm import prompts

__all__ = [
    "DEFAULT_CONTEXT_BUDGET",
    "DEFAULT_MAX_MESSAGES",
    "ContextMeter",
    "Context",
    "estimate_tokens",
    "stable_head",
    "state_block",
    "build_context",
    "dump_context",
]

DEFAULT_CONTEXT_BUDGET = 8000  # estimated tokens (roadmap M2: metered, bounded)
DEFAULT_MAX_MESSAGES = 12  # recent transcript lines kept before trimming


def estimate_tokens(text: str) -> int:
    """Char heuristic: ~4 chars per token (M2; a real tokenizer is an M5 upgrade)."""
    return 0 if not text else math.ceil(len(text) / 4)


def stable_head(world: WorldState, *, spine_now: str = "") -> str:
    """The cacheable system message (the state/memory layout, stable head).

    M3 appends the spine [NOW] block (spine_now); M5 the campaign summary
    (world.summary). NPC agendas and keeper-only details never appear here.
    """
    parts = [
        prompts.DM_IDENTITY,
        prompts.RULES_SUMMARY,
        prompts.GM_STANDARDS,
        prompts.KEEPER_DISCIPLINE,
    ]
    if spine_now:
        parts.append(f"[NOW]\n{spine_now}")
    if world.summary:
        parts.append(f"Campaign summary:\n{world.summary}")
    return "\n\n".join(parts)


def _fmt_inventory(inventory: dict[str, int]) -> str:
    return ", ".join(f"{quantity}x {name}" for name, quantity in sorted(inventory.items()))


def state_block(world: WorldState) -> str:
    """The volatile-tail state section (engine-provided; never spoken).

    Keeper-only data (NPC agendas, keeper notes' source) is excluded by
    construction — this text goes to the model as what the player may know.
    """
    lines = [prompts.STATE_HEADER]
    pc = world.pc
    if pc is not None:
        location = world.locations.get(pc.location)
        heading = f"Location: {pc.location}" if pc.location else "Location: (unlocated)"
        if location is not None and location.description:
            heading += f" — {location.description}"
        lines.append(heading)
        if location is not None and location.exits:
            lines.append(f"Exits: {', '.join(location.exits)}")
        sheet = pc.sheet
        lines.append(
            f"PC: {sheet.name} — level {sheet.level}, HP {sheet.hp}/{sheet.max_hp}, AC {sheet.ac}, "
            f"proficiency {sheet.proficiency}, gold {sheet.gold}, XP {sheet.xp}"
        )
        if sheet.inventory:
            lines.append(f"Inventory: {_fmt_inventory(sheet.inventory)}")
    for name in sorted(world.npcs):
        npc = world.npcs[name]
        line = f"NPC: {name}"
        if npc.location:
            line += f" (in {npc.location})"
        if npc.status != "alive":
            line += f" — {npc.status}"
        if npc.description:
            line += f": {npc.description}"
        lines.append(line)
    for name in sorted(world.quests):
        quest = world.quests[name]
        description = quest.description or "objective unclear"
        lines.append(f"Quest [{quest.status}]: {name} — {description}")
    for name in sorted(world.items):
        item = world.items[name]
        if item.holder:
            where = f"with {item.holder}"
        elif item.location:
            where = f"at {item.location}"
        else:
            where = "location unknown"
        suffix = f" — {item.description}" if item.description else ""
        lines.append(f"Item: {item.name} ({where}){suffix}")
    for fact in world.facts:
        lines.append(f"Fact (turn {fact.turn}): {fact.subject} {fact.predicate} {fact.value}")
    if world.combat is not None:
        combat = world.combat
        parts = []
        for name in combat.order:
            record = combat.participants[name]
            suffix = f" [{', '.join(sorted(record.conditions))}]" if record.conditions else ""
            parts.append(f"{name} HP {record.hp}/{record.max_hp}{suffix}")
        lines.append(f"Combat (round {combat.round}): " + ", ".join(parts))
        lines.append(f"Acting now: {combat.current}")
    return "\n".join(lines)


def _transcript_text(transcript: list[Message]) -> str:
    if not transcript:
        return ""
    lines = ["Recent transcript:"]
    for message in transcript:
        prefix = "You: " if message.role == "user" else "DM: "
        lines.append(prefix + message.content)
    return "\n".join(lines)


@dataclass(frozen=True)
class ContextMeter:
    """The prompt, metered per section (est. tokens; chars/4 heuristic)."""

    stable_head: int
    state_block: int
    transcript: int
    player_line: int
    budget: int

    @property
    def total(self) -> int:
        return self.stable_head + self.state_block + self.transcript + self.player_line

    def fits(self) -> bool:
        return self.total <= self.budget


@dataclass
class Context:
    """The assembled prompt: stable head + volatile tail (state + transcript)."""

    system: str
    state_block: str
    transcript: list[Message]
    meter: ContextMeter
    player_line: str | None = None

    def tail(self, player_line: str | None = None) -> str:
        """The volatile-tail user message (state, transcript, player line)."""
        line = self.player_line if player_line is None else player_line
        parts = [self.state_block]
        transcript = _transcript_text(self.transcript)
        if transcript:
            parts.append(transcript)
        if line is not None:
            parts.append(f"You: {line}")
        return "\n\n".join(parts)

    def messages(self, player_line: str | None = None) -> list[dict[str, str]]:
        """The two prompt messages (system + user) for the provider."""
        return [
            {"role": "system", "content": self.system},
            {"role": "user", "content": self.tail(player_line)},
        ]


def _meter(
    system: str, state: str, transcript: list[Message], line: str | None, budget: int
) -> ContextMeter:
    return ContextMeter(
        stable_head=estimate_tokens(system),
        state_block=estimate_tokens(state),
        transcript=sum(estimate_tokens(m.content) for m in transcript),
        player_line=estimate_tokens(line or ""),
        budget=budget,
    )


def build_context(
    world: WorldState,
    *,
    messages: list[Message] | None = None,
    budget: int = DEFAULT_CONTEXT_BUDGET,
    max_messages: int = DEFAULT_MAX_MESSAGES,
    spine_now: str = "",
    player_line: str | None = None,
) -> Context:
    """Assemble + meter the turn prompt.

    The transcript keeps the last `max_messages` history lines; if the
    metered total still exceeds the budget, oldest lines drop until it fits
    (floor: the last two lines — below that there is nothing left to trim).
    """
    system = stable_head(world, spine_now=spine_now)
    state = state_block(world)
    history = list(messages or [])
    transcript = history[-max_messages:]
    meter = _meter(system, state, transcript, player_line, budget)
    while not meter.fits() and len(transcript) > 2:
        transcript = transcript[1:]
        meter = _meter(system, state, transcript, player_line, budget)
    return Context(
        system=system,
        state_block=state,
        transcript=transcript,
        meter=meter,
        player_line=player_line,
    )


def dump_context(
    world: WorldState,
    *,
    messages: list[Message] | None = None,
    budget: int = DEFAULT_CONTEXT_BUDGET,
    max_messages: int = DEFAULT_MAX_MESSAGES,
    spine_now: str = "",
    player_line: str | None = None,
) -> str:
    """The readable `/dump-context` output: meter table + both messages."""
    context = build_context(
        world,
        messages=messages,
        budget=budget,
        max_messages=max_messages,
        spine_now=spine_now,
        player_line=player_line,
    )
    meter = context.meter
    verdict = "fits" if meter.fits() else "OVER BUDGET"
    lines = [
        f"=== context dump (turn {world.turn}) ===",
        f"stable head   {meter.stable_head:>6} est-tokens",
        f"state block   {meter.state_block:>6} est-tokens",
        f"transcript    {meter.transcript:>6} est-tokens "
        f"({len(context.transcript)}/{max_messages} messages kept)",
        f"player line   {meter.player_line:>6} est-tokens",
        f"total         {meter.total:>6} est-tokens (budget {budget}) — {verdict}",
        "",
        "--- stable head (system message) ---",
        context.system,
        "",
        "--- volatile tail (user message) ---",
        context.tail(player_line),
    ]
    return "\n".join(lines)
