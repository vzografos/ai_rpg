"""Web world tests (M2): player-safe /api/world and debug /api/dump-context.

The endpoints read the world fresh from the event log on each request (the
M2 authority), so a world committed out-of-band (e.g. by the debug CLI in
another container process) is visible immediately — no restart needed.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from _pytest.monkeypatch import MonkeyPatch
from fastapi.testclient import TestClient

from rpg.engine.events import WorldDB, WorldEvent
from rpg.engine.state import WorldState
from rpg.llm import prompts
from rpg.web import app as app_module
from tests.conftest import FakeProvider

SEED = {
    "pc": {
        "name": "Mira",
        "hp": 12,
        "max_hp": 12,
        "ac": 14,
        "abilities": {
            "strength": 10,
            "dexterity": 10,
            "constitution": 10,
            "intelligence": 10,
            "wisdom": 10,
            "charisma": 10,
        },
        "inventory": {"Torch": 1},
        "gold": 5,
        "xp": 0,
    },
    "pc_location": "The tavern",
    "locations": [
        {"name": "The village square", "description": "A quiet square.", "exits": ["The tavern"]},
        {
            "name": "The tavern",
            "description": "Woodsmoke and low voices.",
            "exits": ["The village square"],
        },
    ],
    "npcs": [
        {
            "name": "Brenna",
            "location": "The tavern",
            "description": "The tavern keeper.",
            "agenda": "She is hunting the shadow crown and will not say so.",
        }
    ],
    "quests": [{"name": "Find the shadow crown", "description": "The mayor pays for it."}],
    "items": [],
}


def web_client(monkeypatch: MonkeyPatch, fake: FakeProvider) -> TestClient:
    monkeypatch.setattr(app_module, "build_provider", lambda env=None: fake)
    monkeypatch.setenv("RPG_DATA", tempfile.mkdtemp(prefix="rpg-test-"))
    return TestClient(app_module.app)


def seed_world(db_path: Path) -> None:
    """Commit a world from out-of-band (as the debug CLI would)."""
    with WorldDB(db_path) as db:
        world = WorldState()
        world.apply_event("world_init", SEED, turn=0)
        db.commit(world, [WorldEvent(turn=0, kind="world_init", payload=SEED)], [])


def db_path(monkeypatch: MonkeyPatch) -> Path:
    return Path(os.environ["RPG_DATA"]) / "rpg.db"


def test_world_api_reports_no_world_yet(monkeypatch: MonkeyPatch) -> None:
    with web_client(monkeypatch, FakeProvider()) as c:
        response = c.get("/api/world")
    assert response.status_code == 200
    body = response.json()
    assert body["world"] is None
    assert body["events"] == 0
    assert body["last_snapshot"] is None


def test_world_api_reports_the_committed_world(monkeypatch: MonkeyPatch) -> None:
    with web_client(monkeypatch, FakeProvider()) as c:
        seed_world(db_path(monkeypatch))
        response = c.get("/api/world")
    assert response.status_code == 200
    body = response.json()
    assert body["events"] == 1
    assert body["last_snapshot"]["turn"] == 0
    world = body["world"]
    assert world["pc"]["name"] == "Mira"
    assert world["pc"]["hp"] == 12
    assert world["npcs"]["Brenna"]["location"] == "The tavern"
    assert "Find the shadow crown" in world["quests"]
    # Keeper-only fields are stripped from the browser-facing projection.
    assert "agenda" not in world["npcs"]["Brenna"]
    assert "notes" not in world["npcs"]["Brenna"]


def test_dump_context_is_disabled_by_default(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv("RPG_DEBUG", raising=False)
    with web_client(monkeypatch, FakeProvider()) as c:
        seed_world(db_path(monkeypatch))
        response = c.get("/api/dump-context")
    assert response.status_code == 404
    assert "not found" in response.json()["detail"]


def test_dump_context_endpoint_404s_without_a_world(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("RPG_DEBUG", "1")
    with web_client(monkeypatch, FakeProvider()) as c:
        response = c.get("/api/dump-context")
    assert response.status_code == 404
    assert "no world" in response.json()["detail"]


def test_dump_context_endpoint_serves_the_metered_dump(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("RPG_DEBUG", "1")
    with web_client(monkeypatch, FakeProvider()) as c:
        seed_world(db_path(monkeypatch))
        response = c.get("/api/dump-context")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    dump = response.text
    assert "context dump" in dump
    assert "est-tokens" in dump
    assert prompts.STATE_HEADER in dump
    assert "PC: Mira" in dump
    # Keeper-only data never crosses the wire.
    assert "hunting the shadow crown" not in dump
