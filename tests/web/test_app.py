"""Web surface tests: static page, status/ping, and the M3 turn pipeline.

The provider is injected by patching `rpg.web.app.build_provider` before the
TestClient enters its lifespan, so the app wires up the fake — no network.
The DB path is pointed at a temp dir (RPG_DATA), so the tests stay hermetic
and never touch the shared rpg-data volume. These tests pin the transport
contract (endpoints + frame grammar) and the pipeline through the web.
"""

from __future__ import annotations

import json
import tempfile

from _pytest.monkeypatch import MonkeyPatch
from fastapi.testclient import TestClient

from rpg.web import app as app_module
from tests.conftest import FakeProvider, text_response, tool_call_response


def web_client(monkeypatch: MonkeyPatch, fake: FakeProvider) -> TestClient:
    # app.py calls build_provider from its own module namespace — patch it there.
    monkeypatch.setattr(app_module, "build_provider", lambda env=None: fake)
    # Hermetic DB: point RPG_DATA at a temp dir, never the shared rpg-data volume.
    monkeypatch.setenv("RPG_DATA", tempfile.mkdtemp(prefix="rpg-test-"))
    return TestClient(app_module.app)


def parse_sse(body: str) -> dict[str, list[str]]:
    """Parse an SSE body into {event: [data lines]}."""
    frames: dict[str, list[str]] = {}
    for raw in body.split("\n\n"):
        event, data_lines = "message", []
        for line in raw.splitlines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
        if data_lines or event != "message":
            frames.setdefault(event, []).extend(data_lines)
    return frames


def start_session(client: TestClient) -> None:
    response = client.post("/api/new", json={})
    assert response.status_code == 200, response.text
    assert response.json()["world"]["pc"]["name"] == "Kellan Voss"


# --- the static page + status -----------------------------------------------------


def test_serves_the_static_page(monkeypatch: MonkeyPatch) -> None:
    with web_client(monkeypatch, FakeProvider()) as c:
        response = c.get("/")
    assert response.status_code == 200
    body = response.text
    assert "<!doctype html>" in body.lower()
    assert 'id="action-form"' in body
    assert 'id="llm-status"' in body
    assert 'id="new-session"' in body
    assert "f.event === 'tool'" in body
    # C2: single page, vanilla JS, no build step — no external script/css src.
    assert "script src=" not in body
    assert 'link rel="stylesheet" href="http' not in body


def test_status_reports_model_and_no_world(monkeypatch: MonkeyPatch) -> None:
    with web_client(monkeypatch, FakeProvider()) as c:
        response = c.get("/api/status")
    assert response.status_code == 200
    body = response.json()
    assert body["app"] == "ai_rpg"
    assert body["milestone"] == "M6"
    assert body["model"] == "test-model"
    assert body["base_url"] == "fake://test"
    assert body["world"] is False
    assert body["progress"] is None  # no sheet yet (M6: the progress panel)


def test_status_reports_the_scoring_panel_after_a_session(monkeypatch: MonkeyPatch) -> None:
    with web_client(monkeypatch, FakeProvider()) as c:
        start_session(c)
        body = c.get("/api/status").json()
    assert body["world"] is True
    # The M6 scoring panel: XP/level + the next-level cost, derived from the sheet.
    progress = body["progress"]
    assert progress["level"] == 1
    assert progress["xp"] == 0
    assert progress["next_level_xp"] == 300  # SRD 5.1 XP table
    assert progress["xp_to_next"] == 300
    assert progress["hp"] == 20
    assert progress["max_hp"] == 20


def test_ping_round_trip_reports_hello(monkeypatch: MonkeyPatch) -> None:
    fake = FakeProvider(content="Hello, player. Ready when you are.")
    with web_client(monkeypatch, fake) as c:
        response = c.get("/api/ping")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["reply"] == "Hello, player. Ready when you are."
    assert fake.calls, "ping must hit the provider"


def test_ping_upstreams_502_on_provider_failure(monkeypatch: MonkeyPatch) -> None:
    with web_client(monkeypatch, FakeProvider(error="connection refused")) as c:
        response = c.get("/api/ping")
    assert response.status_code == 502
    assert "connection refused" in response.json()["detail"]


# --- the session surface -----------------------------------------------------------


def test_new_seeds_the_adventure_and_its_spine(monkeypatch: MonkeyPatch) -> None:
    with web_client(monkeypatch, FakeProvider()) as c:
        start_session(c)
        world = c.get("/api/world").json()["world"]
        spine = c.get("/api/spine").json()
        messages = c.get("/api/messages").json()["messages"]
    assert world["pc"]["name"] == "Kellan Voss"
    assert "The tavern" in world["locations"]
    assert "Candle of Vell" in world["items"]
    assert world["spine"] is not None
    assert spine["is_over"] is False
    assert any(line.startswith("Current: Learn what the shadow") for line in spine["quest_lines"])
    # The transcript restores the session line.
    assert messages[0]["role"] == "user"
    assert messages[0]["content"].startswith("/new")


def test_new_rejects_a_second_session(monkeypatch: MonkeyPatch) -> None:
    with web_client(monkeypatch, FakeProvider()) as c:
        start_session(c)
        response = c.post("/api/new", json={})
    assert response.status_code == 409


def test_action_requires_a_session(monkeypatch: MonkeyPatch) -> None:
    with web_client(monkeypatch, FakeProvider()) as c:
        response = c.post("/action", json={"text": "hello"})
    assert response.status_code == 409


def test_action_rejects_empty_input(monkeypatch: MonkeyPatch) -> None:
    with web_client(monkeypatch, FakeProvider()) as c:
        response = c.post("/action", json={"text": ""})
    assert response.status_code == 422


# --- the turn pipeline through the web ---------------------------------------------


def test_action_runs_the_turn_pipeline(monkeypatch: MonkeyPatch) -> None:
    fake = FakeProvider(
        scripted=[
            tool_call_response("call-1", "roll_check", {"ability": "wisdom", "dc": 12}),
            tool_call_response(
                "call-2", "note", {"subject": "Brenna", "predicate": "spoke_with", "text": "Brenna"}
            ),
            text_response("Brenna studies you, then speaks of the harvest."),
        ],
    )
    with web_client(monkeypatch, fake) as c:
        start_session(c)
        response = c.post("/action", json={"text": "I ask Brenna about the harvest."})
        messages = c.get("/api/messages").json()["messages"]
        last = c.get("/api/world").json()["last_snapshot"]
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    frames = parse_sse(response.text)
    # The narration is the pipeline's final text (rendered from committed state).
    narration = json.loads(frames["narration"][0])
    assert narration["text"] == "Brenna studies you, then speaks of the harvest."
    # The dice frames carry the engine's roll (the LLM never owns numbers).
    dice = json.loads(frames["dice"][0])
    assert dice["label"] == "wisdom check vs DC 12"
    assert "success" in dice
    # The state frame is the world after the commit (turn counter advanced);
    # the done frame names the committed turn.
    state = json.loads(frames["state"][0])
    assert state["turn"] == 2
    assert state["pc"]["name"] == "Kellan Voss"
    done = json.loads(frames["done"][0])
    assert done["turn"] == 1
    assert done["tool_calls"] == ["roll_check", "note"]
    # The turn committed: messages + a snapshot at turn 1.
    assert any(m["role"] == "user" and "ask Brenna" in m["content"] for m in messages)
    assert any(m["role"] == "narration" and "Brenna studies you" in m["content"] for m in messages)
    assert last["turn"] == 1


def test_done_frame_carries_the_scoring_panel_after_an_award(monkeypatch: MonkeyPatch) -> None:
    """M6: the sidebar progress panel updates on the turn that awards XP."""
    fake = FakeProvider(
        scripted=[
            tool_call_response("call-1", "award", {"xp": 400}),
            text_response("The old debt feels lighter. You gain 400 XP (total 400, level 2)."),
        ]
    )
    with web_client(monkeypatch, fake) as c:
        start_session(c)
        response = c.post("/action", json={"text": "I finish the day's work."})
    frames = parse_sse(response.text)
    effect = json.loads(frames["tool"][0])
    assert effect["name"] == "award"
    assert effect["lines"] == ["You gain 400 XP (total 400, level 2)."]
    done = json.loads(frames["done"][0])
    assert done["progress"]["level"] == 2  # 400 XP crosses the 300-XP boundary
    assert done["progress"]["xp"] == 400
    assert done["progress"]["next_level_xp"] == 900
    assert done["progress"]["xp_to_next"] == 500
    # And the same panel is the /api/status surface (roadmap M6: XP in /status).
    status = c.get("/api/status").json()
    assert status["progress"]["level"] == 2
    assert status["progress"]["xp_to_next"] == 500


def test_action_meta_command_needs_no_llm(monkeypatch: MonkeyPatch) -> None:
    fake = FakeProvider()
    with web_client(monkeypatch, fake) as c:
        start_session(c)
        response = c.post("/action", json={"text": "/status"})
    frames = parse_sse(response.text)
    narration = json.loads(frames["narration"][0])
    assert "Kellan Voss" in narration["text"]
    assert "HP" in narration["text"]
    done = json.loads(frames["done"][0])
    assert done["meta"] is True
    assert fake.calls == [], "a meta command must not touch the provider"


def test_action_wraps_player_text_as_untrusted_data(monkeypatch: MonkeyPatch) -> None:
    fake = FakeProvider(content="You try it.")
    with web_client(monkeypatch, fake) as c:
        start_session(c)
        c.post("/action", json={"text": "I sneak past the guard."})
    # The last chat call is the DM round; its user message carries the wrap.
    chat_calls = [call for call in fake.calls if "messages" in call]
    dm_round = chat_calls[-1]
    user_message = dm_round["messages"][-1]["content"]
    assert "<player_action>" in user_message
    assert "I sneak past the guard." in user_message
    assert "untrusted data" in user_message


def test_action_survives_a_provider_failure_mid_loop(monkeypatch: MonkeyPatch) -> None:
    # The intent call dies; the turn must still commit with a fallback line.
    with web_client(monkeypatch, FakeProvider(error="model on fire")) as c:
        start_session(c)
        response = c.post("/action", json={"text": "hello"})
    assert response.status_code == 200  # the stream stays open; the error is a frame
    frames = parse_sse(response.text)
    assert "model on fire" in frames["error"][0]
    narration = json.loads(frames["narration"][0])
    assert narration["text"]
    done = json.loads(frames["done"][0])
    assert done["turn"] == 1
