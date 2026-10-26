"""Web surface tests: static page, status/ping, and the SSE frame contract.

The provider is injected by patching `rpg.web.app.build_provider` before the
TestClient enters its lifespan, so the app wires up the fake — no network.
The DB path is pointed at a temp dir (RPG_DATA), so the tests stay hermetic
and never touch the shared rpg-data volume. These tests pin the transport
contract (endpoints + frame grammar).
"""

from __future__ import annotations

import json
import tempfile

from _pytest.monkeypatch import MonkeyPatch
from fastapi.testclient import TestClient

from rpg.web import app as app_module
from tests.conftest import FakeProvider


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


def test_serves_the_static_page(monkeypatch):
    with web_client(monkeypatch, FakeProvider()) as c:
        response = c.get("/")
    assert response.status_code == 200
    body = response.text
    assert "<!doctype html>" in body.lower()
    assert 'id="action-form"' in body
    assert 'id="llm-status"' in body
    # C2: single page, vanilla JS, no build step — no external script/css src.
    assert "script src=" not in body
    assert 'link rel="stylesheet" href="http' not in body


def test_status_reports_model(monkeypatch):
    with web_client(monkeypatch, FakeProvider()) as c:
        response = c.get("/api/status")
    assert response.status_code == 200
    body = response.json()
    assert body["app"] == "ai_rpg"
    assert body["milestone"] == "M2"
    assert body["model"] == "test-model"
    assert body["base_url"] == "fake://test"


def test_ping_round_trip_reports_hello(monkeypatch):
    fake = FakeProvider(content="Hello, player. Ready when you are.")
    with web_client(monkeypatch, fake) as c:
        response = c.get("/api/ping")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["reply"] == "Hello, player. Ready when you are."
    assert fake.calls, "ping must hit the provider"


def test_ping_upstreams_502_on_provider_failure(monkeypatch):
    with web_client(monkeypatch, FakeProvider(error="connection refused")) as c:
        response = c.get("/api/ping")
    assert response.status_code == 502
    assert "connection refused" in response.json()["detail"]


def test_action_streams_sse_frames(monkeypatch):
    with web_client(monkeypatch, FakeProvider(content="You push open the iron door.")) as c:
        response = c.post("/action", json={"text": "I push open the door."})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")

    frames = parse_sse(response.text)
    # Frame grammar: narration carries the model text; done carries usage.
    assert frames["narration"] == ["You push open the iron door."]
    done = json.loads(frames["done"][0])
    assert done["usage"] == 15  # 10 prompt + 5 completion from the fake
    assert done["model"] == "test-model"


def test_action_streams_error_frame_on_provider_failure(monkeypatch):
    with web_client(monkeypatch, FakeProvider(error="model on fire")) as c:
        response = c.post("/action", json={"text": "hello"})
    assert response.status_code == 200  # the stream stays open; the error is a frame
    assert "event: error" in response.text
    assert "model on fire" in response.text


def test_action_rejects_empty_input(monkeypatch):
    with web_client(monkeypatch, FakeProvider()) as c:
        response = c.post("/action", json={"text": ""})
    assert response.status_code == 422


def test_action_wraps_player_text_as_the_user_message(monkeypatch):
    fake = FakeProvider()
    with web_client(monkeypatch, fake) as c:
        c.post("/action", json={"text": "I sneak past the guard."})
    (call,) = fake.calls
    messages = call["messages"]
    assert messages[-1]["role"] == "user"
    assert messages[-1]["content"] == "I sneak past the guard."
