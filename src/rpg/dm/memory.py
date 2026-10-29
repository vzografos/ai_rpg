"""Memory maintenance (roadmap M5): the keeper's recall, folded on every turn.

Three non-blocking steps run after the turn's events are settled and before
the single per-turn commit — their events ride the same transaction:

- rolling summary fold (state/memory layout): due every FOLD_EVERY_TURNS or when the
  context meter is at FOLD_METER_FRACTION of its budget, the transcript
  since the last fold's watermark is folded into the running campaign
  summary (`summary_set`, watermark = the turn being committed). Fold once,
  retry once on overflow; no progress (empty/unparseable) is not retried.
- fact extraction (M5): the turn's durable triples come out of a utility
  call, capped at MAX_FACTS_PER_TURN, deduped, with resolved-fact removal
  (only facts already on record can be removed) — `fact_add`/`fact_remove`
  with source "extraction", recalled later via the FTS5 index.
- spine refresh (story spine, clamped): at a completed beat the model may propose
  bounded deltas — skip a moot beat, annotate a beat, rewrite a
  not-yet-played beat of the current act. Every delta is validated on a
  scratch copy by the same `apply_delta` the event log replays; violations
  are dropped, never fatal.

Nothing here may block a turn: a provider failure or a rejected payload
skips that step (recorded in the turn's errors) and the turn still commits.
"""

from __future__ import annotations

import json
from typing import Any

from rpg.dm.spine import StorySpine
from rpg.engine.context import DEFAULT_CONTEXT_BUDGET
from rpg.engine.events import Message, WorldEvent
from rpg.engine.state import WorldState
from rpg.llm import prompts
from rpg.llm.provider import LLMProvider, ProviderError

__all__ = [
    "FOLD_EVERY_TURNS",
    "FOLD_METER_FRACTION",
    "SUMMARY_MAX_CHARS",
    "MAX_FACTS_PER_TURN",
    "MAX_SPINE_REFRESH_DELTAS",
    "fold_due",
    "fold_history",
    "fold_summary",
    "extract_turn_facts",
    "refresh_spine",
]

FOLD_EVERY_TURNS = 10  # a fold at most every ten committed turns
FOLD_METER_FRACTION = 0.75  # ...or as soon as the context is 3/4 of its budget
SUMMARY_MAX_CHARS = 1500  # the folded summary stays compact (~400 est-tokens)
SUMMARY_RETRY_MAX_CHARS = 900  # the single overflow retry must come back tighter
MAX_FACTS_PER_TURN = 3  # the extraction cap (M5: capped, deduped)
MAX_SPINE_REFRESH_DELTAS = 4  # the clamped-refresh cap (story spine)

# The refresh vocabulary: what the model may do at a chapter close. `done`
# is deliberately absent — beats complete by the server's waypoint ticks
# (M3 pin: the model narrates the story, it never advances it); "skipped"
# is how a beat the chapter made moot is retired.
_REFRESH_OPS = ("beat_status", "append_detail", "rewrite_beat")


def fold_due(world: WorldState, meter_total: int, *, budget: int = DEFAULT_CONTEXT_BUDGET) -> bool:
    """Is a fold due for the turn being committed? (turn count or the meter.)"""
    if world.turn <= 0:
        return False
    if world.turn - world.summary_watermark >= FOLD_EVERY_TURNS:
        return True
    return meter_total >= int(FOLD_METER_FRACTION * budget)


def fold_history(
    world: WorldState, messages: list[Message], *, line: str, narration: str
) -> str:
    """The transcript the fold must cover: the lines after the last watermark,
    plus the in-hand pair of the turn being committed (not in the log yet)."""
    lines: list[str] = []
    for message in messages:
        if message.turn <= world.summary_watermark:
            continue
        lines.append(("You: " if message.role == "user" else "DM: ") + message.content)
    if line:
        lines.append(f"You: {line}")
    if narration:
        lines.append(f"DM: {narration}")
    return "\n".join(lines)


async def fold_summary(
    provider: LLMProvider,
    world: WorldState,
    history_text: str,
    *,
    errors: list[str],
) -> WorldEvent | None:
    """Fold `history_text` into the running summary (roadmap M5: fold-once,
    retry-once on overflow).

    Returns the `summary_set` event to commit (already applied to the world),
    or None when the fold made no progress — the old summary then stays and
    the watermark does not move.
    """
    if not history_text:
        return None
    for overflow in (False, True):  # fold-once, then the single overflow retry
        try:
            data = await provider.extract_json(
                prompts.SUMMARY_FOLD_SYSTEM,
                prompts.summary_fold_user(world.summary, history_text, overflow=overflow),
            )
        except ProviderError as exc:
            errors.append(f"memory fold: {exc}")
            return None
        summary = data.get("summary") if isinstance(data, dict) else None
        if not isinstance(summary, str) or not summary.strip():
            # No progress (empty or unparseable): not retried (roadmap M5) and
            # not an error — the turn is not degraded, the watermark simply
            # does not move and the next due turn folds again.
            return None
        summary = summary.strip()
        limit = SUMMARY_RETRY_MAX_CHARS if overflow else SUMMARY_MAX_CHARS
        if len(summary) > limit:
            if not overflow:
                continue  # the single retry: the same facts, tighter
            return None  # still over after the retry: keep the old summary
        turn = world.turn
        payload = {"summary": summary, "watermark": turn}
        world.apply_event("summary_set", payload, turn)
        return WorldEvent(turn=turn, kind="summary_set", payload=payload)
    raise AssertionError("unreachable: the fold is at most two calls")


def _fact_triple(raw: Any) -> tuple[str, str, str] | None:
    """One durable fact from the model: a non-empty (subject, predicate, value)."""
    if not isinstance(raw, dict):
        return None
    values = (raw.get("subject"), raw.get("predicate"), raw.get("value"))
    if not all(isinstance(value, str) and value.strip() for value in values):
        return None
    return (values[0].strip(), values[1].strip(), values[2].strip())


def _fact_key(triple: tuple[str, str, str]) -> tuple[str, str, str]:
    return (triple[0].casefold(), triple[1].casefold(), triple[2].casefold())


def _event_lines(events: list[WorldEvent]) -> list[str]:
    return [f"{event.kind}: {json.dumps(event.payload, sort_keys=True)}" for event in events]


def _record_lines(world: WorldState) -> list[str]:
    return [
        f"{fact.subject} {fact.predicate} {fact.value} (turn {fact.turn})" for fact in world.facts
    ]


async def extract_turn_facts(
    provider: LLMProvider,
    world: WorldState,
    *,
    line: str,
    narration: str,
    events: list[WorldEvent],
    errors: list[str],
) -> list[WorldEvent]:
    """The turn's durable facts (roadmap M5: capped, deduped, removals clamped).

    Returns the `fact_add`/`fact_remove` events to commit (already applied to
    the world). Invalid entries are dropped silently; a provider failure
    skips the step.
    """
    turn = world.turn
    try:
        data = await provider.extract_json(
            prompts.FACT_EXTRACT_SYSTEM,
            prompts.fact_extract_user(line, narration, _event_lines(events), _record_lines(world)),
        )
    except ProviderError as exc:
        errors.append(f"fact extraction: {exc}")
        return []
    if not isinstance(data, dict):
        return []
    new_events: list[WorldEvent] = []
    seen: set[tuple[str, str, str]] = set()
    existing: set[tuple[str, str, str]] = set()
    for fact in world.facts:
        key = _fact_key((fact.subject, fact.predicate, fact.value))
        seen.add(key)
        existing.add((fact.subject, fact.predicate, fact.value))
    raw_facts = data.get("facts")
    if isinstance(raw_facts, list):
        added = 0
        for raw in raw_facts:
            if added >= MAX_FACTS_PER_TURN:
                break
            triple = _fact_triple(raw)
            if triple is None:
                continue
            key = _fact_key(triple)
            if key in seen:
                continue  # already on record (case-insensitive)
            seen.add(key)
            payload = {
                "subject": triple[0],
                "predicate": triple[1],
                "value": triple[2],
                "source": "extraction",
            }
            world.apply_event("fact_add", payload, turn)
            new_events.append(WorldEvent(turn=turn, kind="fact_add", payload=payload))
            added += 1
    raw_removes = data.get("remove")
    if isinstance(raw_removes, list):
        for raw in raw_removes:
            triple = _fact_triple(raw)
            if triple is None or triple not in existing:
                continue  # removals only retire facts already on record
            seen.discard(_fact_key(triple))
            existing.discard(triple)
            payload = {"subject": triple[0], "predicate": triple[1], "value": triple[2]}
            world.apply_event("fact_remove", payload, turn)
            new_events.append(WorldEvent(turn=turn, kind="fact_remove", payload=payload))
    return new_events


async def refresh_spine(
    provider: LLMProvider,
    world: WorldState,
    *,
    completed_text: str,
    errors: list[str],
) -> list[WorldEvent]:
    """The clamped spine refresh at a completed beat (the story spine).

    Each proposed delta is validated on a scratch copy of the spine by the
    same `apply_delta` the event log replays; accepted deltas are then
    applied to the live spine in the same order (and returned as the
    `spine_delta` events to commit). Violations are dropped, never fatal.
    """
    spine = world.spine
    if spine is None or not completed_text:
        return []
    turn = world.turn
    try:
        data = await provider.extract_json(
            prompts.SPINE_REFRESH_SYSTEM,
            prompts.spine_refresh_user(completed_text, spine.now_block()),
        )
    except ProviderError as exc:
        errors.append(f"spine refresh: {exc}")
        return []
    if not isinstance(data, dict):
        return []
    raw_deltas = data.get("deltas")
    if not isinstance(raw_deltas, list):
        return []
    scratch = StorySpine.from_dict(spine.to_dict())
    accepted: list[dict[str, Any]] = []
    for raw in raw_deltas[:MAX_SPINE_REFRESH_DELTAS]:
        op = raw.get("op") if isinstance(raw, dict) else None
        if op not in _REFRESH_OPS:
            continue
        if op == "beat_status" and raw.get("status") != "skipped":
            continue  # the model retires moot beats as "skipped"; never marks done
        try:
            scratch.apply_delta(op, raw)
        except ValueError as exc:
            errors.append(f"spine refresh: dropped a delta ({exc})")
            continue
        accepted.append(raw)
    new_events: list[WorldEvent] = []
    for payload in accepted:
        world.apply_event("spine_delta", payload, turn)
        new_events.append(WorldEvent(turn=turn, kind="spine_delta", payload=payload))
    return new_events
