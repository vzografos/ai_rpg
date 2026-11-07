"""M3 acceptance: a 10-turn fake-LLM pipeline session through the
web surface, plus the kill-and-restart identity check.

The fake DM is scripted per turn (tool rounds + narration); the dice are
seeded (RPG_SEED) so the whole session is reproducible. The assertions are
the acceptance criteria: no crash in 10 turns, the engine owns every number,
the spine advances through real play (waypoint ticks from committed facts),
and a restart reproduces the identical world from the event log.
"""

from __future__ import annotations

import json
import os
import tempfile

from _pytest.monkeypatch import MonkeyPatch
from fastapi.testclient import TestClient

from rpg.engine.events import WorldDB, rebuild_world
from rpg.web import app as app_module
from tests.conftest import FakeProvider, text_response, tool_call_response
from tests.web.test_app import parse_sse


def note(call_id: str, name: str):
    """A spoke_with fact — the evidence the spine ticks an npc waypoint on."""
    return tool_call_response(
        call_id, "note", {"subject": name, "predicate": "spoke_with", "text": name}
    )


def say(text: str):
    return text_response(text)


# One scripted line per chat round, in order (the intent calls are separate).
SCRIPT = [
    # Turn 1: talk to Brenna (beat 1 waypoint 1: npc Brenna)
    note("t1a", "Brenna"),
    say("Brenna dries a mug and watches you over it, then speaks of the blackened fields."),
    # Turn 2: move to the tavern
    tool_call_response("t2a", "move_to", {"location": "The tavern"}),
    say("The Hearth smells of woodsmoke; Tallow leads you to a corner table."),
    # Turn 3: ask Old Maren about the blight (beat 1 waypoint 2) — beat 1 completes
    tool_call_response("t3a", "roll_check", {"ability": "wisdom", "dc": 12}),
    note("t3b", "Old Maren"),
    say("Old Maren tells you of the last king, mid-crowning, and the candle that unmade it."),
    # Turn 4: plain narration, no tools
    say("You sketch the square in your notebook: chapel, square, fields. The blight "
        "starts at the wall."),
    # Turn 5: to the mayor's hall
    tool_call_response("t5a", "move_to", {"location": "The mayor's hall"}),
    say("Aldous receives you behind a desk of cold ink, his coat too fine for a poor hall."),
    # Turn 6: take the Candle of Vell (beat 2 waypoint 2: item)
    tool_call_response("t6a", "take_item", {"name": "Candle of Vell"}),
    say("The candle is warm in your hand, and the room's cold seems to step back."),
    # Turn 7: press Mayor Aldous (beat 2 waypoint 1) — beat 2 completes
    note("t7a", "Mayor Aldous"),
    say("Aldous admits the seal was sold. The spare key to the undercroft, he says, "
        "stays with Tallow."),
    # Turn 8: down into the undercroft (beat 3 waypoint 1: place)
    tool_call_response("t8a", "move_to", {"location": "The undercroft"}),
    say("Stone steps down, into a cold that smells of wet rock and old incense."),
    # Turn 9: plain narration
    say("You light the candle of Vell. The dark at the bottom of the stairs hesitates."),
    # Turn 10: a strength check at the crypt door (beat 3 in progress)
    tool_call_response("t10a", "roll_check", {"ability": "strength", "dc": 13}),
    say("You set your shoulder against the crypt door and it gives, groaning on iron hinges."),
]


def session_client(monkeypatch: MonkeyPatch, fake: FakeProvider) -> TestClient:
    monkeypatch.setattr(app_module, "build_provider", lambda env=None: fake)
    monkeypatch.setenv("RPG_DATA", tempfile.mkdtemp(prefix="rpg-session-"))
    monkeypatch.setenv("RPG_SEED", "42")  # reproducible dice for the acceptance run
    return TestClient(app_module.app)


def play(client: TestClient, text: str) -> dict[str, list[str]]:
    response = client.post("/action", json={"text": text})
    assert response.status_code == 200, response.text
    frames = parse_sse(response.text)
    assert "narration" in frames, f"no narration for {text!r}: {frames}"
    return frames


def test_ten_turn_fake_llm_session_advances_the_spine(monkeypatch: MonkeyPatch) -> None:
    fake = FakeProvider(scripted=list(SCRIPT))
    with session_client(monkeypatch, fake) as c:
        response = c.post("/api/new", json={})
        assert response.status_code == 200, response.text

        beats = []
        for i, text in enumerate(
            [
                "I ask Brenna about the harvest.",
                "I move to the tavern.",
                "I find Old Maren by the chapel steps and ask her about the blight.",
                "I study the square and the fields.",
                "I go to the mayor's hall.",
                "I take the candle from the mayor's shelf.",
                "I press Mayor Aldous about the seal.",
                "I go down into the undercroft.",
                "I light the candle of Vell.",
                "I force the crypt door open.",
            ],
            start=1,
        ):
            frames = play(c, text)
            done = json.loads(frames["done"][0])
            assert done["turn"] == i
            assert "error" not in frames, f"turn {i} errored: {frames['error']}"
            if "beat" in frames:
                beats.append((i, json.loads(frames["beat"][0])["beat"]["text"]))

        # Beat 1 completed on turn 3, beat 2 on turn 7 — advanced by real play.
        assert beats[0][0] == 3
        assert beats[1][0] == 7
        spine = c.get("/api/spine").json()
        assert spine["is_over"] is False
        assert any(line.startswith("Current: Pass the undercroft") for line in spine["quest_lines"])
        assert "Learn what the shadow" in spine["completed"][0]
        assert "Find the candle" in spine["completed"][1]

        # The world committed: 11 turns (session + 10), one snapshot each.
        world_body = c.get("/api/world").json()
        assert world_body["world"]["turn"] == 11
        assert world_body["events"] >= 11
        # The PC holds the candle and stands in the undercroft.
        world = world_body["world"]
        assert world["pc"]["location"] == "The undercroft"
        assert world["items"]["Candle of Vell"]["holder"] == "pc:Kellan Voss"
        # The transcript is user/narration only (the state/memory layout): 11 turns x 2.
        assert len(c.get("/api/messages").json()["messages"]) == 22

    # Kill and restart: the same db file replays to the identical world.
    db_path = f"{os.environ['RPG_DATA']}/rpg.db"
    with WorldDB(db_path) as db:
        restarted = db.load_world()
        replayed = rebuild_world(db.events())
        replayed.turn = restarted.turn  # the session concept the DB assigns
    assert replayed.fingerprint() == restarted.fingerprint()
    assert replayed.to_dict() == restarted.to_dict()
