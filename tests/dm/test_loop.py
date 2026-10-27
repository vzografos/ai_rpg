"""Turn pipeline unit tests (M3): the meta short-circuit, the tool loop,
the fallback narration (a turn is never lost), the spine advance, and the
frame contract the transport renders.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from rpg.dm.loop import MAX_TOOL_ROUNDS, TurnPipeline
from rpg.dm.spine import adventure_init_payload, load_adventure
from rpg.engine.dice import SeededSource
from rpg.engine.events import Message, WorldDB, WorldEvent
from rpg.engine.state import WorldState
from tests.conftest import FakeProvider, text_response, tool_call_response

ADVENTURE_PATH = (
    Path(__file__).resolve().parents[2] / "src" / "rpg" / "content" / "shadow_crown.json"
)
PC = "Kellan Voss"


def seeded_pipeline(tmp_path, scripted=None, error=None, seed: int = 42, extract_json_result=None):
    db = WorldDB(str(tmp_path / "rpg.db"))
    db.__enter__()
    adventure = load_adventure(ADVENTURE_PATH)
    world = WorldState()
    world.apply_event("world_init", adventure_init_payload(adventure), 0)
    world.apply_event("spine_set", {"spine": adventure.spine.to_dict()}, 0)
    db.commit(
        world,
        [
            WorldEvent(turn=0, kind="world_init", payload=adventure_init_payload(adventure)),
            WorldEvent(turn=0, kind="spine_set", payload={"spine": adventure.spine.to_dict()}),
        ],
        [
            Message(turn=0, role="user", content="New session"),
            Message(turn=0, role="narration", content="Session started."),
        ],
    )
    provider = FakeProvider(scripted=scripted, error=error, extract_json_result=extract_json_result)
    pipeline = TurnPipeline(provider, db, world, SeededSource(seed))
    return db, world, provider, pipeline


def frames_by_type(result) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for event, payload in result.frames:
        out.setdefault(event, []).append(json.loads(payload))
    return out


def test_meta_turn_bypasses_the_llm(tmp_path) -> None:
    db, world, provider, pipeline = seeded_pipeline(tmp_path)
    result = asyncio.run(pipeline.run("/status"))
    assert provider.calls == []  # no LLM at all for a meta command
    assert result.was_meta is True
    assert "Kellan Voss" in result.narration
    frames = frames_by_type(result)
    assert frames["done"] == [{"turn": 1, "usage": {}, "meta": True}]
    assert frames["state"][0]["turn"] == 2  # post-commit: session + this turn
    assert world.turn == 2
    # The command and its reply are both committed (the log stays complete).
    last_two = db.messages()[-2:]
    assert last_two[0].content == "/status" and last_two[0].role == "user"
    assert last_two[1].content == result.narration and last_two[1].role == "narration"


def test_in_game_turn_commits_events_and_renders_the_frames(tmp_path) -> None:
    db, world, provider, pipeline = seeded_pipeline(
        tmp_path,
        scripted=[
            tool_call_response(
                "c1", "note", {"subject": "Brenna", "predicate": "spoke_with", "text": "Brenna"}
            ),
            text_response(
                "Brenna speaks of the blackened fields, "
                "and of a crown that should stay buried."
            ),
        ],
    )
    result = asyncio.run(pipeline.run("I ask Brenna about the harvest."))
    assert result.turn == 1
    assert result.tool_names == ["note"]
    assert result.narration.startswith("Brenna speaks")
    frames = frames_by_type(result)
    assert "error" not in frames
    assert frames["narration"][0]["text"] == result.narration
    assert frames["state"][0]["pc"]["name"] == PC
    assert frames["done"][0]["turn"] == 1
    # The fact committed; the spine ticked Brenna's waypoint on the same turn.
    kinds = [e.kind for e in db.events() if e.turn == 1]
    assert "fact_add" in kinds and "spine_delta" in kinds
    assert world.spine.beat(0).waypoints[0].done is True
    # The transcript holds the wrapped intent, never the raw outcome.
    user_line = db.messages()[-2]
    assert "ask Brenna about the harvest" in user_line.content
    assert user_line.role == "user"


def test_intent_advisory_is_appended_to_the_dm_user_message(tmp_path) -> None:
    db, world, provider, pipeline = seeded_pipeline(
        tmp_path,
        scripted=[text_response("You test the door.")],
        extract_json_result={"check": {"ability": "strength", "dc": 12}, "combat": False},
    )
    asyncio.run(pipeline.run("I push the crypt door."))
    chat_calls = [call for call in provider.calls if "messages" in call]
    user_message = chat_calls[0]["messages"][1]["content"]
    assert "Advisory from the adjudication clerk" in user_message
    assert "strength check (DC 12)" in user_message


def test_ask_clarification_ends_the_turn_with_the_question(tmp_path) -> None:
    db, world, provider, pipeline = seeded_pipeline(
        tmp_path,
        scripted=[
            tool_call_response("c1", "ask_clarification", {"question": "Which door do you mean?"})
        ],
    )
    result = asyncio.run(pipeline.run("I open the door."))
    assert result.tool_names == ["ask_clarification"]
    assert result.narration == "Which door do you mean?"
    frames = frames_by_type(result)
    assert frames["narration"][0]["text"] == "Which door do you mean?"
    # One chat round was spent; the script never needed a narration line.
    assert len([c for c in provider.calls if "messages" in c]) == 1


def test_the_tool_budget_exhaustion_falls_back_and_still_commits(tmp_path) -> None:
    # Eight tool rounds, never a narration: the loop must end, fall back,
    # and commit the turn (a turn is never lost).
    scripted = [
        tool_call_response(f"c{i}", "roll_check", {"ability": "wisdom", "dc": 12})
        for i in range(MAX_TOOL_ROUNDS)
    ]
    db, world, provider, pipeline = seeded_pipeline(tmp_path, scripted=scripted)
    result = asyncio.run(pipeline.run("I look around very carefully."))
    assert result.tool_names.count("roll_check") == MAX_TOOL_ROUNDS
    assert result.errors
    assert "budget" in " ".join(result.errors)
    assert result.narration.endswith("What do you do?")  # the deterministic fallback
    assert world.turn == 2  # session + this committed turn
    frames = frames_by_type(result)
    assert "error" in frames and "narration" in frames and frames["done"][0]["turn"] == 1
    # Every roll's dice frame is carried, so the narration stays honest.
    assert len(frames["dice"]) == MAX_TOOL_ROUNDS


def test_a_provider_failure_keeps_the_turn(tmp_path) -> None:
    db, world, provider, pipeline = seeded_pipeline(tmp_path, error="the model is on fire")
    result = asyncio.run(pipeline.run("I walk into the square."))
    assert "llm: the model is on fire" in result.errors
    assert result.narration == "The moment hangs, undecided. What do you do?"
    assert world.turn == 2
    frames = frames_by_type(result)
    assert "error" in frames and frames["done"][0]["turn"] == 1


def test_spine_advance_emits_the_beat_frame_on_committed_play(tmp_path) -> None:
    # Both of beat one's npc waypoints, in one turn: the beat must close and
    # the next one start, with a beat frame for the client.
    db, world, provider, pipeline = seeded_pipeline(
        tmp_path,
        scripted=[
            tool_call_response(
                "c1", "note", {"subject": "Brenna", "predicate": "spoke_with", "text": "Brenna"}
            ),
            tool_call_response(
                "c2",
                "note",
                {"subject": "Old Maren", "predicate": "spoke_with", "text": "Old Maren"},
            ),
            text_response(
                "Between them, the old story comes together: a crown, and a light."
            ),
        ],
    )
    result = asyncio.run(pipeline.run("I gather the harvest stories from Brenna and Old Maren."))
    assert result.beat is not None
    assert result.beat["text"].startswith("Find the candle")
    frames = frames_by_type(result)
    assert frames["beat"][0]["beat"]["text"].startswith("Find the candle")
    assert any(line.startswith("Learn what the shadow") for line in frames["quest"][0]["completed"])
    assert world.spine.active_beat.text.startswith("Find the candle")
    # The advance is a committed spine_delta, so a replay agrees.
    kinds = [e.payload for e in db.events() if e.kind == "spine_delta"]
    assert {"op": "beat_status", "beat": 0, "status": "done"} in kinds
