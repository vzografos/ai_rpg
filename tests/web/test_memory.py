"""M5 acceptance: a 50+ turn fake-LLM session where the early
turns' facts still shape the late game, the context stays bounded, and a
checkpoint round-trips.

The acceptance criteria, in tests:
- a fact planted on turn 5 is still in play on turn 50: the FTS5 recall for
  the player's line pulls it back into the state block even though the
  bounded recent-tail no longer shows it (10 facts on record, 5 recent);
- the rolling summary fold fires (campaign summary non-empty, watermark
  advances, transcript trimmed to the lines after the watermark);
- the prompt stays within the budget every turn (the meter, and the
  per-turn `over_budget` flag the client sees);
- /save + /load round-trip a campaign (the save is self-contained, the live
  DB is swapped for it), and a kill+restart reproduces the identical world
  (summary and watermark included) from the event log.

The fake DM is scripted per turn (a `note` tool records the quiz fact, then
the narration); the dice are seeded (RPG_SEED) so the run is reproducible.
"""

from __future__ import annotations

import json
import os
import tempfile

from _pytest.monkeypatch import MonkeyPatch
from fastapi.testclient import TestClient

from rpg.engine.context import DEFAULT_CONTEXT_BUDGET
from rpg.engine.events import WorldDB, rebuild_world
from rpg.llm import prompts
from rpg.web import app as app_module
from tests.conftest import FakeProvider, LLMResponse, text_response, tool_call_response
from tests.web.test_app import parse_sse

TOTAL_TURNS = 52
LATE_TURN = 50
LATE_LINE = "I ask the villagers what became of the bandit chief who died in the square."

# The quiz facts: one every five turns (10 on record by turn 50, so the
# bounded recent-tail — the last five — no longer shows the turn-5 fact:
# only the FTS5 recall for the late line can bring it back).
PLANTS: dict[int, tuple[str, str, str]] = {
    5: ("The bandit chief", "died", "in the square"),
    10: ("The old lantern", "was found", "in the tavern cellar"),
    15: ("The chapel bell", "is cracked", "and silent"),
    20: ("The signet ring", "was stolen", "from the mayor"),
    25: ("The ferryman", "demands payment", "in bone coins"),
    30: ("The crypt seal", "bears the crown sigil", "and will not open"),
    35: ("The hooded figure", "was seen", "at the river bend"),
    40: ("The crypt key", "is hidden", "in the chapel"),
    45: ("The mayor", "fled", "to the river gate"),
    50: ("The shadow crown", "is rumored", "to be buried beneath the crypt"),
}

# The canned fold output: the keeper's running summary of the campaign.
QUIZ_SUMMARY = (
    "The bandit chief died in the square. The signet ring was stolen from the mayor. "
    "The crypt key is hidden in the chapel. The hunt for the shadow crown continues."
)

EXTRACT_ROUTES = {
    prompts.INTENT_SYSTEM: {"check": None, "combat": False},
    prompts.SUMMARY_FOLD_SYSTEM: {"summary": QUIZ_SUMMARY},
    prompts.FACT_EXTRACT_SYSTEM: {"facts": [], "remove": []},
    prompts.SPINE_REFRESH_SYSTEM: {"deltas": []},
}


def build_script() -> list[LLMResponse]:
    """One scripted line per chat round, in order (the intent calls are separate)."""
    script: list[LLMResponse] = []
    for turn in range(1, TOTAL_TURNS + 1):
        if turn in PLANTS:
            subject, predicate, value = PLANTS[turn]
            script.append(
                tool_call_response(
                    f"t{turn}a", "note", {"subject": subject, "predicate": predicate, "text": value}
                )
            )
            script.append(
                text_response(f"You mark it in your journal: {subject} {predicate} {value}.")
            )
        elif turn % 2 == 1:
            script.append(text_response("You press on along the road, and the light holds."))
        else:
            script.append(text_response("You keep moving; the village sounds stay behind you."))
    return script


def player_line(turn: int) -> str:
    if turn == LATE_TURN:
        return LATE_LINE
    return f"I keep moving toward the {['square', 'tavern', 'hall', 'chapel', 'crypt'][turn % 5]}."


def session_client(monkeypatch: MonkeyPatch, fake: FakeProvider) -> TestClient:
    monkeypatch.setattr(app_module, "build_provider", lambda env=None: fake)
    monkeypatch.setenv("RPG_DATA", tempfile.mkdtemp(prefix="rpg-memory-"))
    monkeypatch.setenv("RPG_SEED", "42")  # reproducible dice for the acceptance run
    monkeypatch.setenv("RPG_DEBUG", "1")  # the context meter is a debug surface
    return TestClient(app_module.app)


def play(client: TestClient, text: str) -> dict[str, list[str]]:
    response = client.post("/action", json={"text": text})
    assert response.status_code == 200, response.text
    frames = parse_sse(response.text)
    assert "narration" in frames, f"no narration for {text!r}: {frames}"
    return frames


def meter_total(dump: str) -> int:
    """The 'total N est-tokens' line of a /api/dump-context body."""
    for line in dump.splitlines():
        if line.startswith("total "):
            return int(line.split()[1])
    raise AssertionError(f"no meter total in dump:\n{dump}")


def fake_provider() -> FakeProvider:
    return FakeProvider(scripted=build_script(), extract_json_routes=EXTRACT_ROUTES)


def test_fifty_plus_turn_memory_quiz(monkeypatch: MonkeyPatch) -> None:
    fake = fake_provider()
    with session_client(monkeypatch, fake) as c:
        response = c.post("/api/new", json={})
        assert response.status_code == 200, response.text

        for turn in range(1, TOTAL_TURNS + 1):
            frames = play(c, player_line(turn))
            done = json.loads(frames["done"][0])
            assert done["turn"] == turn, f"turn {turn} committed {done['turn']}"
            assert "error" not in frames, f"turn {turn} errored: {frames['error']}"
            # The client sees the budget verdict every turn: the prompt fit.
            assert done["over_budget"] is False
            # The metered total (the debug view) stays within the budget too.
            assert meter_total(c.get("/api/dump-context").text) <= DEFAULT_CONTEXT_BUDGET

        world = c.get("/api/world").json()["world"]

        # (a) The rolling summary folded: non-empty, watermark advanced past
        # the early turns (a fold is due every FOLD_EVERY_TURNS turns).
        assert world["summary"] == QUIZ_SUMMARY
        assert world["summary_watermark"] == 50

        # (b) The turn-5 fact is still in play on the late turn: the FTS5
        # recall pulled it into the state block for the DM even though the
        # bounded recent-tail (the last five facts) no longer shows it.
        dm_calls = [
            call
            for call in fake.calls
            if call.get("tools")
            and any(LATE_LINE in (message.get("content") or "") for message in call["messages"])
        ]
        assert dm_calls, "no DM round carried the late player line"
        system = dm_calls[0]["messages"][0]["content"]
        tail = dm_calls[0]["messages"][1]["content"]
        state = tail.split("Recent transcript:")[0]  # the state block section only
        assert "Fact (turn 5): The bandit chief died in the square" in state
        # ...and the bounded state block left the old, unrecalled facts out.
        assert "old lantern" not in state
        assert "signet ring" not in state
        # The folded summary rides the stable head (cacheable system message).
        assert QUIZ_SUMMARY in system

        # (c) Every quiz fact is a committed engine fact (the log, not a
        # model hallucination).
        fact_lines = [
            f"{fact['subject']} {fact['predicate']} {fact['value']}" for fact in world["facts"]
        ]
        for subject, predicate, value in PLANTS.values():
            assert f"{subject} {predicate} {value}" in fact_lines

    # Kill and restart: the log replays to the identical world (the folded
    # summary and its watermark ride `summary_set` events in the log).
    db_path = f"{os.environ['RPG_DATA']}/rpg.db"
    with WorldDB(db_path) as db:
        restarted = db.load_world()
        replayed = rebuild_world(db.events())
        replayed.turn = restarted.turn  # the session concept the DB assigns
    assert replayed.fingerprint() == restarted.fingerprint()
    assert replayed.to_dict() == restarted.to_dict()
    assert replayed.summary == restarted.summary == QUIZ_SUMMARY
    assert replayed.summary_watermark == restarted.summary_watermark == 50


def test_save_and_load_round_trip(monkeypatch: MonkeyPatch) -> None:
    fake = fake_provider()
    with session_client(monkeypatch, fake) as c:
        assert c.post("/api/new", json={}).status_code == 200

        for turn in range(1, 21):  # play through turn 20 (a fold fired at 10 and 20)
            frames = play(c, player_line(turn))
            assert json.loads(frames["done"][0])["turn"] == turn

        # Checkpoint the campaign (the world counter is the next turn: 21).
        saved = c.post("/api/save", json={"name": "midpoint"}).json()
        assert saved == {"ok": True, "name": "midpoint", "turn": 21}
        assert "midpoint" in c.get("/api/sessions").json()["saves"]
        at_save = c.get("/api/world").json()["world"]
        assert at_save["turn"] == 21
        assert at_save["summary"] == QUIZ_SUMMARY
        assert at_save["summary_watermark"] == 20

        # Keep playing: the live world moves on past the checkpoint.
        for turn in range(21, 31):
            frames = play(c, player_line(turn))
            assert json.loads(frames["done"][0])["turn"] == turn
        assert c.get("/api/world").json()["world"]["turn"] == 31

        # Resume the checkpoint: the live DB is swapped for the save.
        loaded = c.post("/api/load", json={"name": "midpoint"}).json()
        assert loaded["ok"] is True and loaded["turn"] == 21
        assert loaded["world"]["turn"] == at_save["turn"]
        assert loaded["world"]["facts"] == at_save["facts"]
        assert loaded["world"]["summary"] == at_save["summary"]
        assert c.get("/api/sessions").json()["turn"] == 21

        # Bad names are rejected (the save naming is one rule, engine + web).
        assert c.post("/api/save", json={"name": "bad name!"}).status_code == 400
        assert c.post("/api/load", json={"name": "nope"}).status_code == 404

    # The save file is a self-contained, resumable campaign: the resumed
    # world equals the (swapped) live DB, folded memory included.
    save_path = f"{os.environ['RPG_DATA']}/saves/midpoint.db"
    with WorldDB(save_path) as db:
        resumed = db.load_world()
    with WorldDB(f"{os.environ['RPG_DATA']}/rpg.db") as db:
        live_after = db.load_world()
    assert resumed.fingerprint() == live_after.fingerprint()
    assert resumed.summary == QUIZ_SUMMARY
    assert resumed.summary_watermark == 20
