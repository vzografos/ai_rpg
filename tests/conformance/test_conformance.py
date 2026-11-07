"""M6 conformance adventure (milestone M6, eval layer 1): the test suite plays
`shadow_crown` end-to-end with a fake LLM and seeded dice — the campaign
must complete (the finale beat reached) **deterministically**, no model involved.

This is the engine-conformance layer: it proves the pre-authored adventure is
actually *winnable* through the real pipeline (every beat advances on committed
evidence, both combats resolve by the engine, the XP/level scoring falls out),
and that a restart replays the log to the byte-identical world. The dice seed
is pinned so the whole run is reproducible; a different seed is a different
campaign, which is why the seed is part of the contract, not an accident.
"""

from __future__ import annotations

import json
import os
import tempfile

from _pytest.monkeypatch import MonkeyPatch
from fastapi.testclient import TestClient

from rpg.engine.events import WorldDB, rebuild_world
from rpg.llm import prompts
from rpg.web import app as app_module
from tests.conftest import FakeProvider, text_response, tool_call_response
from tests.web.test_app import parse_sse

SEED = 14  # pinned: the PC wins both combats and the spine completes (see scan note)

WIGHT = {
    "name": "The Grave Wight",
    "ac": 11,
    "hp": 14,
    "max_hp": 14,
    "dexterity": 10,
    "attack_bonus": 2,
    "damage": "1d6+1",
}
VESPER = {
    "name": "Vesper the Hollow",
    "ac": 13,
    "hp": 20,
    "max_hp": 20,
    "dexterity": 12,
    "attack_bonus": 3,
    "damage": "1d6+2",
}


def note(call_id: str, name: str, predicate: str = "spoke_with"):
    return tool_call_response(
        call_id, "note", {"subject": name, "predicate": predicate, "text": name}
    )


def say(text: str):
    return text_response(text)


# One scripted line per chat round, in order. The player lines (PLAYER_LINES)
# are what a real player would type; the DM's tool rounds + narration are canned.
SCRIPT = [
    # T1: talk to Brenna (beat 1 wp1: npc Brenna)
    note("t1a", "Brenna"),
    say("Brenna dries a mug and watches you over it, then speaks of the blackened fields."),
    # T2: move to the tavern
    tool_call_response("t2a", "move_to", {"location": "The tavern"}),
    say("The Hearth smells of woodsmoke; Tallow leads you to a corner table."),
    # T3: ask Old Maren about the blight (beat 1 wp2) — beat 1 completes
    note("t3a", "Old Maren"),
    say("Old Maren tells you of the last king, mid-crowning, and the candle that unmade it."),
    # T4: to the mayor's hall
    tool_call_response("t4a", "move_to", {"location": "The mayor's hall"}),
    say("Aldous receives you behind a desk of cold ink, his coat too fine for a poor hall."),
    # T5: take the Candle of Vell (beat 2 wp2: item)
    tool_call_response("t5a", "take_item", {"name": "Candle of Vell"}),
    say("The candle is warm in your hand, and the room's cold seems to step back."),
    # T6: press Mayor Aldous (beat 2 wp1) — beat 2 completes
    note("t6a", "Mayor Aldous"),
    say("Aldous admits the seal was sold; the spare key to the undercroft stays with Tallow."),
    # T7: down into the undercroft (beat 3 wp1: place)
    tool_call_response("t7a", "move_to", {"location": "The undercroft"}),
    say("Stone steps down, into a cold that smells of wet rock and old incense."),
    # T8: a wisdom read on Sable (beat 3 wp2: npc Sable)
    tool_call_response("t8a", "roll_check", {"ability": "wisdom", "dc": 12}),
    note("t8b", "Sable"),
    say("Sable's eyes finally meet yours. He opened the crypt door for a stranger, he says."),
    # T9: the wight (beat 3 wp3: fight) — beat 3 completes; the PC rests to recover
    tool_call_response("t9a", "start_combat", {"participants": [WIGHT]}),
    tool_call_response("t9b", "end_combat", {"outcome": "victory", "xp_award": 300}),
    note("t9c", "The Grave Wight", "defeated"),
    say("The wight's shape folds into the dark. The altar stands open, and the crown is there."),
    # T10: down into the King's Crypt
    tool_call_response("t10a", "move_to", {"location": "The King's Crypt"}),
    say("You light the candle of Vell. The dark at the foot of the tomb draws back."),
    # T11: rest (recover), then take the crown (beat 4 wp1: item The Shadow Crown)
    tool_call_response("t11a", "rest", {"kind": "long"}),
    tool_call_response("t11b", "take_item", {"name": "The Shadow Crown"}),
    say("You rest a while, then lift the circlet from the dead king's brow."),
    # T12: Vesper (beat 4 wp2: fight) — beat 4 completes
    tool_call_response("t12a", "start_combat", {"participants": [VESPER]}),
    tool_call_response("t12b", "end_combat", {"outcome": "victory", "xp_award": 500}),
    note("t12c", "Vesper the Hollow", "defeated"),
    say("Vesper's voice thins to a whisper, and the prophet's grey goes down like smoke."),
    # T13: unmake the crown (beat 5 wp1: objective)
    tool_call_response("t13a", "note", {"subject": "the player", "predicate": "completed",
                                        "text": "The shadow crown is unmade"}),
    say("The candle's light takes the circlet apart, ring by ring, into nothing."),
    # T14: back to the village square (beat 5 wp2: place)
    tool_call_response("t14a", "move_to", {"location": "The village square"}),
    say("Lanterns are already going up in the square as the harvest moon rises clear."),
    # T15: the harvest is saved (beat 5 wp3: narrative) — finale beat completes
    note("t15a", "The harvest is saved", "occurred"),
    say("The fields stand straight in the dark. Whatever else is owed, this night is over."),
]

PLAYER_LINES = [
    "I ask Brenna about the harvest.",
    "I head to the tavern.",
    "I find Old Maren by the chapel steps and ask her about the blackened fields.",
    "I go to the mayor's hall.",
    "I take the candle from the shelf.",
    "I press Mayor Aldous about the seal.",
    "I go down into the undercroft.",
    "I talk to Sable, the grave-digger.",
    "I confront the thing guarding the altar.",
    "I go down into the King's Crypt.",
    "I rest, then take the crown from the altar.",
    "I face Vesper at the altar.",
    "I unmake the crown in the candle's light.",
    "I climb back up to the village square.",
    "I carry the guttering candle into the square as the harvest moon rises.",
]

# The canned memory steps (fold/facts/refresh) resolve to no-ops so the run is
# pure engine behaviour: no invented facts, no spine drift, a known summary.
ROUTES = {
    prompts.SUMMARY_FOLD_SYSTEM: {
        "summary": (
            "Kellan Voss learns of the shadow crown's blight, takes the candle of Vell from "
            "the mayor's hall, passes the undercroft into the King's Crypt, unmade the crown, "
            "and brought the light back to the village square."
        )
    },
    prompts.FACT_EXTRACT_SYSTEM: {"facts": [], "remove": []},
    prompts.SPINE_REFRESH_SYSTEM: {"deltas": []},
}


def session_client(monkeypatch: MonkeyPatch, fake: FakeProvider) -> TestClient:
    monkeypatch.setattr(app_module, "build_provider", lambda env=None: fake)
    monkeypatch.setenv("RPG_DATA", tempfile.mkdtemp(prefix="rpg-conformance-"))
    monkeypatch.setenv("RPG_SEED", str(SEED))  # reproducible dice for the conformance run
    return TestClient(app_module.app)


def play(client: TestClient, text: str) -> dict[str, list[str]]:
    response = client.post("/action", json={"text": text})
    assert response.status_code == 200, response.text
    frames = parse_sse(response.text)
    assert "narration" in frames, f"no narration for {text!r}: {frames}"
    return frames


def run_campaign(client: TestClient) -> dict[str, object]:
    """Play the whole adventure; return the observable end state."""
    beats: list[tuple[int, str]] = []
    for i, text in enumerate(PLAYER_LINES, start=1):
        frames = play(client, text)
        done = json.loads(frames["done"][0])
        assert done["turn"] == i, f"turn mismatch: expected {i}, got {done['turn']}"
        assert "error" not in frames, f"turn {i} errored: {frames['error']}"
        if "beat" in frames:
            beats.append((i, json.loads(frames["beat"][0])["beat"]["text"]))
    spine = client.get("/api/spine").json()
    world = client.get("/api/world").json()["world"]
    return {"beats": beats, "spine": spine, "world": world}


def test_conformance_adventure_completes_deterministically(monkeypatch: MonkeyPatch) -> None:
    fake = FakeProvider(scripted=list(SCRIPT), extract_json_routes=dict(ROUTES))
    with session_client(monkeypatch, fake) as c:
        response = c.post("/api/new", json={})
        assert response.status_code == 200, response.text
        result = run_campaign(c)

    beats = result["beats"]
    spine = result["spine"]
    world = result["world"]

    # The story advanced on committed evidence, in order: each chapter close
    # announces the newly active beat (M3: the `beat` frame is the beat that
    # started this turn) — the finale's completion has no successor, so the
    # campaign's end is read from the spine projection, not a frame.
    assert [turn for turn, _ in beats] == [3, 6, 9, 12]
    assert [text for _, text in beats] == [
        "Find the candle that can unmake the crown",
        "Pass the undercroft into the King's Crypt",
        "Take the shadow crown from the dead king",
        "Unmake the crown and bring the light back",
    ]
    assert spine["is_over"] is True  # the finale beat (5 of 5) is done
    assert len(spine["completed"]) == 5
    assert spine["completed"][-1] == "Unmake the crown and bring the light back"

    # The campaign actually completed: the PC is home, the crown is held,
    # the finale beat's waypoints are all done.
    assert world["pc"]["location"] == "The village square"
    assert world["items"]["The Shadow Crown"]["holder"] == "pc:Kellan Voss"
    final_beat = world["spine"]["beats"][-1]
    assert final_beat["status"] == "done"
    assert all(waypoint["done"] for waypoint in final_beat["waypoints"])

    # The XP/level scoring fell out of the engine (M6): 800 XP, level 2 —
    # the /api/status progress panel is the designated surface for it.
    assert world["pc"]["xp"] == 800
    status = c.get("/api/status").json()
    assert status["progress"]["xp"] == 800
    assert status["progress"]["level"] == 2
    assert status["progress"]["xp_to_next"] == 900 - 800  # the next-level cost

    # The combat was resolved by the engine, not narrated: the wight and Vesper
    # are on record as defeated facts (the spine's fight waypoints tick on them).
    facts = {(f["subject"], f["predicate"], f["value"]) for f in world["facts"]}
    assert ("The Grave Wight", "defeated", "The Grave Wight") in facts
    assert ("Vesper the Hollow", "defeated", "Vesper the Hollow") in facts


def test_conformance_replay_is_byte_identical(monkeypatch: MonkeyPatch) -> None:
    """Rebuild-from-events + a restart on the same file replay to the same world."""
    fake = FakeProvider(scripted=list(SCRIPT), extract_json_routes=dict(ROUTES))
    with session_client(monkeypatch, fake) as c:
        assert c.post("/api/new", json={}).status_code == 200
        result = run_campaign(c)
        db_path = f"{os.environ['RPG_DATA']}/rpg.db"

    live = result["world"]
    with WorldDB(db_path) as db:
        restarted = db.load_world()
        replayed = rebuild_world(db.events())
        replayed.turn = restarted.turn  # the session concept the DB assigns
    assert restarted.to_player_dict() == live, "restart replay diverged from the live world"
    assert replayed.fingerprint() == restarted.fingerprint()
    assert replayed.to_dict() == restarted.to_dict()
