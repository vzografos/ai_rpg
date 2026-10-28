"""The DM loop (the turn pipeline) — the sandwich, per turn:

  1. arbitrate    the player's text: meta (engine, no LLM) or in-game intent
  2. intent       one cheap JSON utility call: does the action need a check
                  or start a fight? (advisory; the engine stays authoritative)
  3. context      the bounded, stable/volatile prompt (M2 context builder,
                  with the spine [NOW] block in the stable head)
  4. DM round     tool-calling loop: the DM narrates with words but resolves
                  with tools; results come back authoritative
   5. adjudicate   the engine already did (every mutation is a validated,
                   clamped, event-logged apply_event)
   6. backstop     the structural wall (M4): forgery/contradiction/identity/
                   combat-transition/secret-leak checks on the narration; a
                   violation triggers one Stop-form re-ask (no tools, "the turn
                   does not end; here is the reason"), re-verified after each
                   re-ask, bounded (3); then a deterministic fallback rendered
                   from the committed events
   7. commit       the event log + tables exactly once (the turn is the unit
                   of persistence; a crash mid-turn replays the turn)
  8. spine        tick the active beat's waypoints, advance on completion
  9. render       the narration from committed state as SSE frames

The LLM never owns numbers (C3): the narration is written from the
authoritative tool results and must reflect them verbatim. The turn is
never lost: a provider failure mid-loop falls back to a deterministic
narration built from the events that already committed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from rpg.dm import arbiter
from rpg.dm.tools import DM_TOOLS, ToolEngine
from rpg.engine.backstops import (
    MAX_BACKSTOP_REASKS,
    fallback_narration,
    run_backstops,
)
from rpg.engine.context import build_context
from rpg.engine.dice import SeededSource, SystemSource
from rpg.engine.events import Message, WorldDB, WorldEvent
from rpg.engine.sheet import ABILITIES
from rpg.engine.state import WorldState
from rpg.llm import prompts
from rpg.llm.provider import LLMProvider, ProviderError, Usage

__all__ = ["MAX_TOOL_ROUNDS", "TurnPipeline", "TurnResult"]

MAX_TOOL_ROUNDS = 8  # the tool loop budget (M3 acceptance: 20-turn crash-free)


@dataclass
class TurnResult:
    """What the pipeline committed and what the transport renders."""

    turn: int
    narration: str = ""
    frames: list[tuple[str, str]] = field(default_factory=list)
    dice: list[dict] = field(default_factory=list)
    tool_names: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    beat: dict | None = None  # the new active beat, when one started this turn
    usage: dict = field(default_factory=dict)
    was_meta: bool = False
    over_budget: bool = False  # the context no longer fits its meter


class TurnPipeline:
    """One pipeline per session; the transport (web/CLI) owns the loop."""

    def __init__(
        self,
        provider: LLMProvider,
        db: WorldDB,
        world: WorldState,
        dice_source: SeededSource | SystemSource | None = None,
        *,
        backstops_enabled: bool = True,
    ) -> None:
        self.provider = provider
        self.db = db
        self.world = world
        self.dice = dice_source or SystemSource()
        self.engine = ToolEngine(world, self.dice)
        # M4: the structural wall. The red-line suite runs the same pipeline
        # with this off to prove each backstop earns its keep.
        self.backstops_enabled = backstops_enabled

    # ------------------------------------------------------------------ turn

    async def run(self, player_text: str) -> TurnResult:
        world = self.world
        turn = world.turn  # the turn this transaction commits (M2 contract)
        result = TurnResult(turn=turn)
        decision = arbiter.arbitrate(player_text, world)
        if decision.is_meta:
            result.was_meta = True
            result.narration = decision.reply
            result.dice = list(decision.dice)
            self.db.commit(
                world,
                [],
                [
                    Message(turn=turn, role="user", content=decision.text),
                    Message(turn=turn, role="narration", content=decision.reply),
                ],
            )
            self._render(result, turn)
            return result
        return await self._run_game_turn(result, decision.text, turn)

    async def _run_game_turn(
        self, result: TurnResult, wrapped_line: str, turn: int
    ) -> TurnResult:
        world = self.world
        self.engine.begin_turn()
        # 2. intent — the cheap advisory call (never fatal).
        intent = await self._decide_intent(world, wrapped_line)
        # 3. context — stable head (with the spine [NOW]) + volatile tail.
        context = build_context(
            world,
            messages=self.db.messages(),
            spine_now=world.spine.now_block() if world.spine is not None else None,
            player_line=wrapped_line,
        )
        result.over_budget = not context.meter.fits()
        llm_messages: list[dict] = [
            {"role": "system", "content": context.system},
            {"role": "user", "content": context.messages()[1]["content"]},
        ]
        if intent is not None:
            llm_messages[1]["content"] += f"\n\nAdvisory from the adjudication clerk: {intent}"
        # 4-5. the DM tool loop (adjudication happened as tools applied).
        try:
            narration, exhausted = await self._tool_loop(llm_messages, result)
        except ProviderError as exc:
            self._error(result, f"llm: {exc}")
            narration, exhausted = None, True
        if exhausted:
            self._error(result, "the DM ran out of tool rounds before narrating (budget)")
        # 6. the backstops: structural checks, bounded Stop-form re-asks,
        # then the deterministic fallback (the turn is never lost).
        result.narration = await self._backstop((narration or "").strip(), llm_messages, result)
        # 7. the turn's events: the tool events the engine applied, plus the
        # spine's own deltas (waypoint ticks, beat advance) below.
        events = [
            WorldEvent(turn=turn, kind=kind, payload=payload)
            for kind, payload in self.engine.events
        ]
        # 8. the spine: tick the waypoints the world just justified, THEN
        # advance the beat (advancing checks the post-tick state).
        spine = world.spine
        if spine is not None:
            for payload in spine.progress_events(world):
                world.apply_event("spine_delta", payload, turn=turn)
                events.append(WorldEvent(turn=turn, kind="spine_delta", payload=payload))
            advances = spine.advance_events()
            for payload in advances:
                world.apply_event("spine_delta", payload, turn=turn)
                events.append(WorldEvent(turn=turn, kind="spine_delta", payload=payload))
            if advances:
                beat = spine.active_beat
                result.beat = beat.to_dict() if beat is not None else None
        # 7/9. commit exactly once (the turn is the unit of persistence),
        # then render the narration from committed state.
        self.db.commit(
            world,
            events,
            [
                Message(turn=turn, role="user", content=wrapped_line),
                Message(turn=turn, role="narration", content=result.narration),
            ],
        )
        self._render(result, turn)
        return result

    # ---------------------------------------------------------- the tool loop

    async def _tool_loop(
        self, messages: list[dict], result: TurnResult
    ) -> tuple[str | None, bool]:
        """Run the DM round(s). Returns (narration, tool_budget_exhausted)."""
        for _ in range(MAX_TOOL_ROUNDS):
            response = await self.provider.chat(messages, tools=DM_TOOLS)
            self._add_usage(result.usage, response.usage)
            if not response.tool_calls:
                return (response.content or "").strip() or None, False
            calls = response.tool_calls
            messages.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": json.dumps(call.arguments),
                            },
                        }
                        for call in calls
                    ],
                }
            )
            for call in calls:
                outcome = self.engine.dispatch(call.name, call.arguments)
                result.tool_names.append(outcome.name)
                result.dice.extend(outcome.dice)
                if not outcome.ok:
                    self._error(result, f"tool {outcome.name}: {outcome.message}")
                feedback = {"ok": outcome.ok, "message": outcome.message}
                if outcome.dice:
                    feedback["dice"] = outcome.dice
                if outcome.authoritative:
                    feedback["authoritative"] = outcome.authoritative
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps(feedback),
                    }
                )
                if outcome.ok and outcome.ends_turn:
                    return outcome.message, False
        return None, True

    # ---------------------------------------------------------------- helpers

    async def _decide_intent(self, world: WorldState, wrapped_line: str) -> str | None:
        """Step 2: the advisory check/combat guess (the turn pipeline). Non-fatal."""
        try:
            data = await self.provider.extract_json(
                prompts.INTENT_SYSTEM, prompts.intent_user(wrapped_line, _summary(world))
            )
        except ProviderError:
            return None
        if not isinstance(data, dict):
            return None
        hints: list[str] = []
        check = data.get("check")
        if isinstance(check, dict):
            ability, dc = check.get("ability"), check.get("dc")
            dc_ok = isinstance(dc, int) and not isinstance(dc, bool) and 1 <= dc <= 30
            if ability in ABILITIES and dc_ok:
                hints.append(f"the action likely needs a {ability} check (DC {dc})")
        if data.get("combat") is True:
            hints.append("hostile creatures are engaging the player — start a combat")
        return "; ".join(hints) or None

    async def _backstop(self, narration: str, llm_messages: list[dict], result: TurnResult) -> str:
        """Step 6: the structural wall (roadmap M4).

        Check the narration against the committed state; on a violation, one
        Stop-form re-ask (tools disabled: "the turn does not end; here is the
        reason"), re-verified after each re-ask, bounded (MAX_BACKSTOP_REASKS);
        then the deterministic fallback rendered from the committed events.
        """
        if not self.backstops_enabled:
            return narration
        violations = run_backstops(
            narration,
            self.world,
            dice=result.dice,
            events=self.engine.events,
            authoritative=self.engine.authoritative,
        )
        if not violations:
            return narration
        # The wall fired: the client sees which backstops caught it (SSE
        # error frame) whether the rewrite succeeds or the fallback carries.
        caught = ", ".join(sorted({violation.kind for violation in violations}))
        reasks = 0
        while violations:
            if reasks >= MAX_BACKSTOP_REASKS:
                self._error(
                    result,
                    f"backstop: {caught} — deterministic fallback "
                    f"after {MAX_BACKSTOP_REASKS} re-asks",
                )
                return fallback_narration(self.engine.authoritative)
            reasks += 1
            stop_messages = llm_messages + [
                {"role": "assistant", "content": narration},
                {"role": "user", "content": prompts.stop_reask([v.detail for v in violations])},
            ]
            try:
                response = await self.provider.chat(stop_messages)
            except ProviderError as exc:
                self._error(result, f"backstop re-ask: {exc}")
                return fallback_narration(self.engine.authoritative)
            self._add_usage(result.usage, response.usage)
            if response.tool_calls:
                self._error(
                    result, "backstop re-ask returned tool calls (the stop form is text-only)"
                )
                continue
            rewritten = (response.content or "").strip()
            if not rewritten:
                self._error(result, f"backstop: {caught} — the re-ask came back empty")
                return fallback_narration(self.engine.authoritative)
            narration = rewritten
            violations = run_backstops(
                narration,
                self.world,
                dice=result.dice,
                events=self.engine.events,
                authoritative=self.engine.authoritative,
            )
        reask_word = "re-ask" if reasks == 1 else "re-asks"
        self._error(result, f"backstop: {caught} — rewritten after {reasks} {reask_word}")
        return narration

    def _error(self, result: TurnResult, message: str) -> None:
        if message not in result.errors:
            result.errors.append(message)

    @staticmethod
    def _add_usage(total: dict, new: Usage) -> None:
        total["prompt_tokens"] = total.get("prompt_tokens", 0) + new.prompt_tokens
        total["completion_tokens"] = total.get("completion_tokens", 0) + new.completion_tokens

    def _render(self, result: TurnResult, turn: int) -> None:
        """Step 9: the SSE frames (the grammar in web/app.py)."""
        world = self.world
        for error in result.errors:
            result.frames.append(("error", json.dumps({"error": error})))
        if result.beat is not None:
            result.frames.append(("beat", json.dumps({"beat": result.beat})))
        if world.spine is not None:
            active = world.spine.active_beat
            result.frames.append(
                (
                    "quest",
                    json.dumps(
                        {
                            "quest_lines": world.spine.quest_lines(),
                            "beat": result.beat
                            if result.beat is not None
                            else (active.to_dict() if active is not None else None),
                            "completed": [b.text for b in world.spine.beats if b.status == "done"],
                        }
                    ),
                )
            )
        for die in result.dice:
            result.frames.append(("dice", json.dumps(die)))
        result.frames.append(("state", json.dumps(world.to_dict())))
        result.frames.append(("narration", json.dumps({"text": result.narration})))
        if result.was_meta:
            result.frames.append(
                ("done", json.dumps({"turn": turn, "usage": {}, "meta": True}))
            )
        else:
            result.frames.append(
                (
                    "done",
                    json.dumps(
                        {
                            "turn": turn,
                            "usage": result.usage,
                            "tool_calls": result.tool_names,
                            "over_budget": result.over_budget,
                        }
                    ),
                )
            )


def _summary(world: WorldState) -> str:
    """The one-line world summary for the intent call (cheap, advisory)."""
    if world.pc is None:
        return "no character yet"
    parts = [f"location: {world.pc.location or '(unlocated)'}"]
    nearby = [n.name for n in world.npcs.values() if n.location == world.pc.location]
    if nearby:
        parts.append("nearby NPCs: " + ", ".join(nearby))
    if world.combat is not None:
        parts.append("a combat is in progress")
    return "; ".join(parts)
