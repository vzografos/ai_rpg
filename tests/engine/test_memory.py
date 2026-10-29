"""M5 memory tests: the rolling summary fold, fact extraction, the clamped
spine refresh, the watermark transcript trim, and the FTS5 recall + saves
(the WorldDB's M5 surface).

The LLM is a fake provider throughout (no network, hermetic): the canned
extract_json responses route by system prompt (conftest) or come off a FIFO
queue (the fold's retry path).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from rpg.dm import memory
from rpg.engine.context import (
    DEFAULT_CONTEXT_BUDGET,
    RECENT_FACTS,
    build_context,
    state_block,
)
from rpg.engine.events import Message, WorldDB, WorldEvent
from rpg.engine.state import WorldState
from rpg.llm import prompts
from tests.conftest import FakeProvider

INITIAL = {
    "pc": {
        "name": "Mira",
        "hp": 12,
        "max_hp": 12,
        "ac": 14,
        "abilities": {"strength": 10},
        "inventory": {},
        "gold": 5,
        "xp": 0,
    },
    "pc_location": "Square",
    "locations": [{"name": "Square", "description": "", "exits": []}],
    "npcs": [{"name": "Brenna", "location": "Square", "agenda": "keeper-only secret"}],
    "quests": [],
    "items": [],
}

SPINE = {
    "premise": "The harvest moon has turned black.",
    "stakes": "The village starves.",
    "antagonist": "The shadow crown.",
    "beats": [
        {
            "text": "Learn of the blight",
            "status": "done",
            "act": 1,
            "waypoints": [{"kind": "npc", "text": "Brenna", "done": True}],
            "detail": "",
        },
        {
            "text": "Find the candle",
            "status": "active",
            "act": 1,
            "waypoints": [{"kind": "item", "text": "Candle of Vell", "done": False}],
            "detail": "",
        },
        {
            "text": "Unmake the crown",
            "status": "pending",
            "act": 2,
            "waypoints": [{"kind": "fight", "text": "The wight", "done": False}],
            "detail": "",
        },
    ],
    "cast": [],
    "events": [],
    "finale": "The fields stand straight in the dark.",
}


def world_with_spine() -> WorldState:
    world = WorldState()
    world.apply_event("world_init", INITIAL, turn=0)
    world.apply_event("spine_set", {"spine": SPINE}, turn=0)
    world.turn = 5  # mid-campaign: the turns so far are what the upkeep sees
    return world


def transcript(turns: list[int]) -> list[Message]:
    return [
        Message(turn=turn, role=role, content=f"line {turn} {role}")
        for turn in turns
        for role in ("user", "narration")
    ]


# --- the fold: due-ness and history ---------------------------------------------------


def test_fold_due_by_turn_count() -> None:
    world = world_with_spine()
    assert memory.fold_due(world, 100) is False  # turn 5, watermark 0: 5 < 10
    world.turn = 10
    assert memory.fold_due(world, 100) is True  # 10 - 0 >= FOLD_EVERY_TURNS
    world.summary_watermark = 10
    assert memory.fold_due(world, 100) is False  # the last fold resets the counter


def test_fold_due_by_the_context_meter() -> None:
    world = world_with_spine()
    threshold = int(memory.FOLD_METER_FRACTION * DEFAULT_CONTEXT_BUDGET)
    assert memory.fold_due(world, threshold - 1) is False
    assert memory.fold_due(world, threshold) is True
    assert memory.fold_due(world, 0) is False  # turn 0 never folds


def test_fold_history_covers_since_the_watermark_plus_the_in_hand_pair() -> None:
    world = world_with_spine()
    world.summary = "An earlier fold."
    world.summary_watermark = 3
    history = memory.fold_history(world, transcript([1, 2, 3, 4, 5]), line="L", narration="N")
    assert "line 3 user" not in history  # at the watermark: folded already
    assert "line 4 user" in history and "line 5 narration" in history
    assert history.endswith("You: L\nDM: N")  # the in-hand pair is covered


# --- the fold: one call, one overflow retry, no-progress never retried ------------------


def test_fold_commits_a_summary_set_event() -> None:
    world = world_with_spine()
    provider = FakeProvider(extract_json_routes={prompts.SUMMARY_FOLD_SYSTEM: {"summary": "S"}})
    errors: list[str] = []
    event = asyncio.run(memory.fold_summary(provider, world, "the history", errors=errors))
    assert event is not None and event.kind == "summary_set"
    assert event.payload == {"summary": "S", "watermark": world.turn}
    assert world.summary == "S" and world.summary_watermark == 5
    assert errors == []
    assert len([c for c in provider.calls if c.get("extract_json")]) == 1


def test_fold_no_progress_is_silent_and_not_retried() -> None:
    world = world_with_spine()
    provider = FakeProvider()  # the default intent dict: no "summary" key
    errors: list[str] = []
    event = asyncio.run(memory.fold_summary(provider, world, "the history", errors=errors))
    assert event is None
    assert world.summary == "" and world.summary_watermark == 0  # the old state stays
    assert errors == []  # not a player-visible error: the turn is not degraded
    assert len(provider.calls) == 1  # no retry


def test_fold_overflow_retries_once_and_comes_back_tighter() -> None:
    world = world_with_spine()
    provider = FakeProvider(
        extract_json_queue=[{"summary": "x" * (memory.SUMMARY_MAX_CHARS + 1)}, {"summary": "tight"}]
    )
    errors: list[str] = []
    event = asyncio.run(memory.fold_summary(provider, world, "the history", errors=errors))
    assert event is not None and event.payload["summary"] == "tight"
    assert errors == []
    assert len(provider.calls) == 2  # fold-once, retry-once
    assert "overflow" in provider.calls[1]["user"].lower() or "tighter" in provider.calls[1]["user"]


def test_fold_still_over_after_the_retry_keeps_the_old_summary() -> None:
    world = world_with_spine()
    world.summary = "The old one."
    world.summary_watermark = 4
    provider = FakeProvider(
        extract_json_queue=[
            {"summary": "x" * 2000},
            {"summary": "y" * (memory.SUMMARY_MAX_CHARS + 1)},
        ]
    )
    errors: list[str] = []
    event = asyncio.run(memory.fold_summary(provider, world, "the history", errors=errors))
    assert event is None
    assert world.summary == "The old one." and world.summary_watermark == 4
    assert len(provider.calls) == 2


def test_fold_provider_failure_is_an_error_and_skips_the_step() -> None:
    world = world_with_spine()
    provider = FakeProvider(error="llm down")
    errors: list[str] = []
    event = asyncio.run(memory.fold_summary(provider, world, "the history", errors=errors))
    assert event is None
    assert errors and "memory fold" in errors[0]


def test_fold_summary_set_rejects_a_watermark_past_its_own_turn() -> None:
    world = world_with_spine()
    with pytest.raises(ValueError, match="watermark"):
        world.apply_event("summary_set", {"summary": "S", "watermark": 99}, turn=5)


# --- fact extraction: capped, deduped, removals clamped ---------------------------------


def _fact_provider(facts: list[dict], remove: list[dict] | None = None) -> FakeProvider:
    return FakeProvider(
        extract_json_routes={
            prompts.FACT_EXTRACT_SYSTEM: {"facts": facts, "remove": remove or []}
        }
    )


def test_extracted_facts_commit_with_the_extraction_source() -> None:
    world = world_with_spine()
    provider = _fact_provider(
        [
            {"subject": "Sable", "predicate": "died", "value": "on the stairs"},
            {"subject": "Tallow", "predicate": "stole", "value": "the spare key"},
        ]
    )
    errors: list[str] = []
    events = asyncio.run(
        memory.extract_turn_facts(
            provider, world, line="I search.", narration="You search.", events=[], errors=errors
        )
    )
    assert [e.kind for e in events] == ["fact_add", "fact_add"]
    assert all(e.payload["source"] == "extraction" for e in events)
    assert [(f.subject, f.value) for f in world.facts] == [
        ("Sable", "on the stairs"),
        ("Tallow", "the spare key"),
    ]
    assert errors == []


def test_extraction_is_capped_and_deduped() -> None:
    world = world_with_spine()
    world.apply_event(
        "fact_add",
        {"subject": "Sable", "predicate": "DIED", "value": "On the Stairs"},
        turn=4,
    )
    provider = _fact_provider(
        [
            {"subject": "Sable", "predicate": "died", "value": "on the stairs"},  # dup (case)
            *[{"subject": f"F{i}", "predicate": "was", "value": f"seen {i}"} for i in range(5)],
        ]
    )
    events = asyncio.run(
        memory.extract_turn_facts(
            provider, world, line="x", narration="y", events=[], errors=[]
        )
    )
    assert len(events) == memory.MAX_FACTS_PER_TURN  # the cap, not 5
    subjects = [e.payload["subject"] for e in events]
    assert "Sable" not in subjects  # already on record (case-insensitive)


def test_removals_retire_only_recorded_facts() -> None:
    world = world_with_spine()
    world.apply_event(
        "fact_add",
        {"subject": "Tallow", "predicate": "holds", "value": "the spare key"},
        turn=3,
    )
    provider = _fact_provider(
        facts=[],
        remove=[
            {"subject": "Tallow", "predicate": "holds", "value": "the spare key"},
            {"subject": "Nobody", "predicate": "was", "value": "here"},  # never recorded
        ],
    )
    events = asyncio.run(
        memory.extract_turn_facts(provider, world, line="x", narration="y", events=[], errors=[])
    )
    assert len(events) == 1
    assert events[0].kind == "fact_remove"
    assert events[0].payload == {
        "subject": "Tallow",
        "predicate": "holds",
        "value": "the spare key",
    }
    assert world.facts == []


def test_extraction_drops_invalid_entries_and_survives_bad_shapes() -> None:
    world = world_with_spine()
    provider = _fact_provider(
        [
            {"subject": "Only", "predicate": "two"},  # no value
            {"subject": "Good one", "predicate": "kept", "value": "the value"},
            "not an object",
            42,
        ]
    )
    events = asyncio.run(
        memory.extract_turn_facts(provider, world, line="x", narration="y", events=[], errors=[])
    )
    assert len(events) == 1 and events[0].payload["subject"] == "Good one"
    # A non-dict response skips the step entirely (no crash, no events).
    provider = FakeProvider(extract_json_routes={prompts.FACT_EXTRACT_SYSTEM: "nonsense"})
    events = asyncio.run(
        memory.extract_turn_facts(provider, world, line="x", narration="y", events=[], errors=[])
    )
    assert events == []
    # A provider failure records an error and skips the step.
    provider = FakeProvider(error="llm down")
    errors: list[str] = []
    events = asyncio.run(
        memory.extract_turn_facts(
            provider, world, line="x", narration="y", events=[], errors=errors
        )
    )
    assert events == [] and errors and "fact extraction" in errors[0]


# --- the clamped spine refresh ------------------------------------------------------------


def _refresh_provider(deltas: list[dict]) -> FakeProvider:
    return FakeProvider(extract_json_routes={prompts.SPINE_REFRESH_SYSTEM: {"deltas": deltas}})


def test_refresh_applies_only_the_clamped_ops() -> None:
    world = world_with_spine()
    provider = _refresh_provider(
        [
            {"op": "append_detail", "beat": 1, "text": "The candle hums."},
            {"op": "beat_status", "beat": 2, "status": "skipped"},  # a moot beat retires
            {"op": "beat_status", "beat": 1, "status": "done"},  # the model never marks done
            {"op": "tick_waypoint", "beat": 1, "waypoint": 0},  # waypoints are the server's
        ]
    )
    errors: list[str] = []
    events = asyncio.run(
        memory.refresh_spine(provider, world, completed_text="Find the candle", errors=errors)
    )
    assert len(events) == 2
    spine = world.spine
    assert spine is not None
    assert spine.beat(1).detail == "The candle hums."
    assert spine.beat(2).status == "skipped"
    assert spine.beat(1).status == "active"  # never marked done by the model
    assert spine.beat(1).waypoints[0].done is False  # never ticked by the model
    assert errors == []


def test_refresh_cannot_rewrite_final_or_foreign_act_beats() -> None:
    world = world_with_spine()
    provider = _refresh_provider(
        [
            {"op": "rewrite_beat", "beat": 0, "text": "A rewritten past."},  # final
            {"op": "rewrite_beat", "beat": 2, "text": "Another act's beat."},  # act 2 vs active
            {"op": "rewrite_beat", "beat": 1, "text": "Find the candle in the mayor's hall."},
        ]
    )
    errors: list[str] = []
    events = asyncio.run(
        memory.refresh_spine(provider, world, completed_text="Learn of the blight", errors=errors)
    )
    spine = world.spine
    assert spine is not None
    assert len(events) == 1
    assert spine.beat(1).text == "Find the candle in the mayor's hall."
    assert spine.beat(0).text == "Learn of the blight"  # the completed beat is intact
    assert spine.beat(2).text == "Unmake the crown"
    assert any("spine refresh" in e for e in errors)  # the clamped drops are recorded


def test_refresh_is_capped_and_skips_when_there_is_no_completion() -> None:
    world = world_with_spine()
    provider = _refresh_provider(
        [{"op": "append_detail", "beat": 2, "text": f"note {i}"} for i in range(6)]
    )
    events = asyncio.run(
        memory.refresh_spine(provider, world, completed_text="Find the candle", errors=[])
    )
    assert len(events) == memory.MAX_SPINE_REFRESH_DELTAS
    # No completed beat: no refresh at all (not even a provider call).
    provider = _refresh_provider([])
    events = asyncio.run(
        memory.refresh_spine(provider, world, completed_text="", errors=[])
    )
    assert events == [] and provider.calls == []
    # A provider failure skips the step.
    provider = FakeProvider(error="llm down")
    errors: list[str] = []
    events = asyncio.run(
        memory.refresh_spine(provider, world, completed_text="Find the candle", errors=errors)
    )
    assert events == [] and errors and "spine refresh" in errors[0]


# --- the context: watermark trim + bounded facts -----------------------------------------


def test_transcript_lines_at_the_watermark_are_replaced_by_the_summary() -> None:
    world = world_with_spine()
    messages = transcript([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
    # Without a fold: the recent window keeps the last 12 lines of the 20.
    context = build_context(world, messages=messages, player_line="You: L")
    assert len(context.transcript) == 12
    # With a fold through turn 5: only the later lines survive (10 of them).
    world.summary = "Turns 1-5, folded."
    world.summary_watermark = 5
    context = build_context(world, messages=messages, player_line="You: L")
    assert len(context.transcript) == 10
    assert all(message.turn > 5 for message in context.transcript)
    assert "Turns 1-5, folded." in context.system  # the summary rides the stable head


def test_no_summary_means_no_watermark_trim() -> None:
    world = world_with_spine()
    messages = transcript([1, 2])
    context = build_context(world, messages=messages, player_line="You: L")
    assert len(context.transcript) == 4  # the early lines are kept (nothing folded them)


def test_state_block_bounds_the_facts_to_the_recall_plus_recent() -> None:
    world = world_with_spine()
    for i in range(12):
        world.apply_event(
            "fact_add",
            {"subject": f"Fact{i}", "predicate": "was", "value": f"noted {i}"},
            turn=i + 1,
        )
    # The dump (no recall): every fact on record.
    block = state_block(world)
    assert all(f"Fact (turn {i + 1}): Fact{i}" in block for i in range(12))
    # A turn with a recall: the recalled facts + the most recent RECENT_FACTS.
    line = lambda i: f"Fact (turn {i + 1}): Fact{i} was noted {i}"  # noqa: E731
    recalled = [world.facts[0], world.facts[1]]
    block = state_block(world, recalled_facts=recalled)
    assert line(0) in block and line(1) in block
    for i in range(2, 12 - RECENT_FACTS):
        assert line(i) not in block  # the middle of the record is out
    for i in range(12 - RECENT_FACTS, 12):
        assert line(i) in block  # the recent tail stays
    # A recalled fact is not duplicated by the recent tail.
    recalled = [world.facts[-1]]
    block = state_block(world, recalled_facts=recalled)
    assert block.count(line(11)) == 1


# --- the WorldDB's M5 surface: summary view, FTS5 recall, saves ---------------------------


def _db_with_facts_and_summary(tmp_path: Path) -> tuple[WorldDB, WorldState]:
    db = WorldDB(tmp_path / "w.db")
    db.__enter__()
    world = WorldState()
    world.apply_event("world_init", INITIAL, turn=0)
    db.commit(world, [WorldEvent(turn=0, kind="world_init", payload=INITIAL)], [])
    facts = [
        (1, {"subject": "Sable", "predicate": "died", "value": "on the undercroft stairs"}),
        (2, {"subject": "Tallow", "predicate": "stole", "value": "the mayor's spare key"}),
    ]
    for turn, payload in facts:  # one transaction per turn (the M2 contract)
        world.apply_event("fact_add", payload, turn)
        db.commit(
            world,
            [WorldEvent(turn=turn, kind="fact_add", payload=payload)],
            [Message(turn=turn, role="user", content=f"line {turn}")],
        )
    summary_payload = {"summary": "Sable died; Tallow holds the key.", "watermark": 2}
    world.apply_event("summary_set", summary_payload, world.turn)
    db.commit(world, [WorldEvent(turn=world.turn, kind="summary_set", payload=summary_payload)], [])
    return db, world


def test_the_summary_view_agrees_between_the_log_and_the_tables(tmp_path: Path) -> None:
    db, live = _db_with_facts_and_summary(tmp_path)
    via_log = db.load_world()
    via_tables = db.load_from_tables()
    assert via_log is not None and via_tables is not None
    assert via_log.summary == live.summary == via_tables.summary
    assert via_log.summary_watermark == live.summary_watermark == via_tables.summary_watermark == 2
    db.close()


def test_recall_facts_matches_ranks_and_respects_the_limit(tmp_path: Path) -> None:
    db, _ = _db_with_facts_and_summary(tmp_path)
    recalled = db.recall_facts("who holds the mayor's spare key?")
    assert [f.subject for f in recalled] == ["Tallow"]
    recalled = db.recall_facts("Sable")
    assert [f.subject for f in recalled] == ["Sable"]
    assert recalled[0].turn == 1  # the UNINDEXED turn round-trips
    # A stopword-only query recalls nothing (no terms to match).
    assert db.recall_facts("the a of") == []
    # The limit bounds the answer.
    recalled = db.recall_facts("Sable Tallow", limit=1)
    assert len(recalled) == 1
    db.close()


def test_backup_and_restore_round_trip_a_whole_campaign(tmp_path: Path) -> None:
    db, live = _db_with_facts_and_summary(tmp_path)
    save = tmp_path / "saves" / "mid.db"
    db.backup_to(save)
    assert save.exists()
    with WorldDB(save) as saved:  # the save is self-contained (multi-session)
        resumed = saved.load_world()
        assert resumed is not None
        assert resumed.fingerprint() == live.fingerprint()
        assert resumed.summary == live.summary
    # The live db keeps moving on while the save stays put...
    world = db.load_world()
    assert world is not None
    world.apply_event("pc_gold", {"amount": 5}, world.turn)
    db.commit(
        world,
        [WorldEvent(turn=world.turn, kind="pc_gold", payload={"amount": 5})],
        [
            Message(turn=world.turn, role="user", content="later"),
            Message(turn=world.turn, role="narration", content="later"),
        ],
    )
    # ...and /load restores the save over the live db.
    db.restore_from(save)
    restored = db.load_world()
    assert restored is not None
    assert restored.fingerprint() == resumed.fingerprint()
    db.close()
