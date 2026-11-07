"""FastAPI app: the transport-agnostic game server (the documented layout, constraint C2).

M6 surface:
    GET  /                   the single static page (chat pane + sidebar, vanilla JS)
    GET  /api/status         app + model info + the scoring panel (milestone M6;
                             the provider endpoint stays server-side)
    GET  /api/ping           one-shot LLM round-trip ("LLM says hello")
    POST /api/new            start a session: seed the pre-authored adventure
    POST /api/save           checkpoint the campaign (named save; default turn-N)
    POST /api/load           resume a save (swaps the live DB, rebinds)
    GET  /api/sessions       the named saves + the live session's turn
    POST /action             player input -> the turn pipeline -> SSE frames
    GET  /api/spine          the player-safe spine projection (quest log)
    GET  /api/messages       the committed transcript (restore on reload)
    GET  /api/world          the player-safe committed world (event-log rebuild)
    GET  /api/dump-context   the metered context dump (debug; RPG_DEBUG=1 only)

The frame grammar (sse_frame) is the contract: narration, dice, state, quest,
beat, tool, error, done. The browser, the in-container debug CLI, and the test
harness parse the same frames — the pipeline never learns which client it
serves. Turn-level streaming (not token-level): the pipeline commits the turn,
then renders its frames.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

from rpg import __version__, session
from rpg.dm.loop import TurnPipeline
from rpg.engine.context import dump_context
from rpg.engine.dice import SeededSource, SystemSource
from rpg.engine.events import Message, WorldDB, default_db_path
from rpg.engine.sheet import xp_progress
from rpg.engine.state import WorldState
from rpg.llm import prompts
from rpg.llm.provider import LLMProvider, ProviderConfig, ProviderError

STATIC_DIR = Path(__file__).parent / "static"
SSE_MEDIA_TYPE = "text/event-stream"


def build_provider(env: dict[str, str] | None = None) -> LLMProvider:
    return LLMProvider(ProviderConfig.from_env(env))


def sse_frame(event: str, data: str | dict[str, Any]) -> str:
    """One SSE frame. The frame grammar is the contract M3 renders against."""
    payload = data if isinstance(data, str) else json.dumps(data)
    return f"event: {event}\ndata: {payload}\n\n"


def _dice_source() -> SeededSource | SystemSource:
    """RPG_SEED makes a session reproducible (acceptance runs); else system."""
    raw = os.environ.get("RPG_SEED", "").strip()
    if raw:
        try:
            return SeededSource(int(raw))
        except ValueError:
            pass
    return SystemSource()


def _debug_enabled() -> bool:
    """Debug endpoints are opt-in: set RPG_DEBUG=1 (the dev compose service does)."""
    return os.environ.get("RPG_DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.provider = build_provider()
    app.state.db = WorldDB(default_db_path())
    app.state.world = app.state.db.load_world()  # the turn pipeline's live world
    app.state.pipeline: TurnPipeline | None = None
    yield
    app.state.db.close()
    await app.state.provider.aclose()


app = FastAPI(title="ai_rpg", version=__version__, lifespan=lifespan)


class ActionIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class NewIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)


class SaveIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=40)


class LoadIn(BaseModel):
    name: str = Field(min_length=1, max_length=40)


def _live_world(request: Request) -> WorldState:
    """The pipeline's world: the one object the DB commits into.

    If the in-memory world is empty but the DB holds a session (seeded
    out-of-band, e.g. the debug CLI), rebind from the log.
    """
    db: WorldDB = request.app.state.db
    world: WorldState | None = request.app.state.world
    if world is None or world.is_empty:
        loaded = db.load_world()
        if loaded is not None:
            request.app.state.world = loaded
            request.app.state.pipeline = None  # rebind the pipeline to the new world
            return loaded
    if world is None:
        world = WorldState()
        request.app.state.world = world
    return world


def _pipeline(request: Request) -> TurnPipeline:
    world = _live_world(request)
    if world.is_empty:
        raise HTTPException(status_code=409, detail="no world yet — start a session: POST /api/new")
    pipeline: TurnPipeline | None = request.app.state.pipeline
    if pipeline is None:
        pipeline = TurnPipeline(
            request.app.state.provider, request.app.state.db, world, _dice_source()
        )
        request.app.state.pipeline = pipeline
    return pipeline


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


def _scoring_panel(world: WorldState | None) -> dict[str, Any] | None:
    """The M6 progress panel (XP/level + next-level cost), when a sheet exists."""
    if world is None or world.is_empty or world.pc is None:
        return None
    sheet = world.pc.sheet
    return {**xp_progress(sheet), "hp": sheet.hp, "max_hp": sheet.max_hp, "gold": sheet.gold}


@app.get("/api/status")
async def status(request: Request) -> dict[str, Any]:
    provider: LLMProvider = request.app.state.provider
    world: WorldState | None = request.app.state.world
    return {
        "app": "ai_rpg",
        "version": __version__,
        "milestone": "M6",
        "model": provider.config.model,
        "world": bool(world is not None and not world.is_empty),
        "progress": _scoring_panel(world),
    }


@app.get("/api/ping")
async def ping(request: Request) -> dict[str, Any]:
    provider: LLMProvider = request.app.state.provider
    try:
        reply = await provider.chat(
            [
                {"role": "system", "content": prompts.PING_SYSTEM},
                {"role": "user", "content": prompts.PING_USER},
            ],
            temperature=0.0,
            max_tokens=80,
        )
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"ok": True, "model": provider.config.model, "reply": (reply.content or "").strip()}


@app.post("/api/new")
async def new_session(body: NewIn, request: Request) -> dict[str, Any]:
    """Start a session: seed the pre-authored adventure + its spine (turn 0)."""
    world = _live_world(request)
    if not world.is_empty:
        raise HTTPException(status_code=409, detail="a session is already running in this database")
    db: WorldDB = request.app.state.db
    provider: LLMProvider = request.app.state.provider
    try:
        world, _ = session.seed_adventure(db, pc_name=body.name, model=provider.config.model)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    request.app.state.world = world
    request.app.state.pipeline = None
    return {"ok": True, "turn": world.turn, "world": world.to_player_dict()}


@app.post("/api/save")
async def save_session(body: SaveIn, request: Request) -> dict[str, Any]:
    """Checkpoint the campaign: a named save the player can /load later."""
    world = _live_world(request)
    if world.is_empty:
        raise HTTPException(status_code=409, detail="no world yet — start a session: POST /api/new")
    db: WorldDB = request.app.state.db
    try:
        name = session.save_session(db, world, body.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "name": name, "turn": world.turn}


@app.post("/api/load")
async def load_session(body: LoadIn, request: Request) -> dict[str, Any]:
    """Resume a save: swap the live DB for it and rebind the session."""
    db: WorldDB = request.app.state.db
    world = _live_world(request)
    name = body.name.strip()
    if not session.save_path(name).exists():
        saves = ", ".join(session.list_saves()) or "none"
        raise HTTPException(status_code=404, detail=f"no such save: {name} (available: {saves})")
    if not world.is_empty:
        # Record the switch in the campaign being left, then swap (M5).
        turn = world.turn
        db.commit(
            world,
            [],
            [
                Message(turn=turn, role="user", content=f"/load {name}"),
                Message(turn=turn, role="narration", content="Switching to the saved campaign."),
            ],
        )
    try:
        world = session.load_session(db, name)
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    request.app.state.world = world
    request.app.state.pipeline = None
    return {"ok": True, "name": name, "turn": world.turn, "world": world.to_player_dict()}


@app.get("/api/sessions")
async def sessions(request: Request) -> dict[str, Any]:
    """The named saves + the live session (M5: multi-session, restart resume)."""
    world: WorldState | None = request.app.state.world
    return {
        "saves": session.list_saves(),
        "turn": None if (world is None or world.is_empty) else world.turn,
        "world": bool(world is not None and not world.is_empty),
    }


@app.post("/action")
async def action(body: ActionIn, request: Request) -> StreamingResponse:
    """The turn pipeline: commit once, then render the committed turn as frames."""
    pipeline = _pipeline(request)

    async def frames() -> AsyncIterator[str]:
        result = await pipeline.run(body.text)
        if result.session_changed:
            # /load or /new rebinds the pipeline's world: follow it.
            request.app.state.world = pipeline.world
        for event, data in result.frames:
            yield sse_frame(event, data)

    return StreamingResponse(
        frames(),
        media_type=SSE_MEDIA_TYPE,
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/spine")
async def spine_projection(request: Request) -> dict[str, Any]:
    """The player-safe spine (the sidebar quest log): no agendas, no details."""
    world = _live_world(request)
    if world.is_empty or world.spine is None:
        raise HTTPException(status_code=404, detail="no spine yet — start a session: POST /api/new")
    spine = world.spine
    active = spine.active_beat
    return {
        "quest_lines": spine.quest_lines(),
        "beat": active.player_dict() if active is not None else None,
        "completed": [beat.text for beat in spine.beats if beat.status == "done"],
        "is_over": spine.is_over,
    }


@app.get("/api/messages")
async def messages(request: Request) -> dict[str, Any]:
    """The committed transcript (user lines + narrations), for reload restore."""
    db: WorldDB = request.app.state.db
    return {
        "messages": [
            {"turn": message.turn, "role": message.role, "content": message.content}
            for message in db.messages()
        ]
    }


@app.get("/api/world")
async def world_status(request: Request) -> dict[str, Any]:
    """The player-safe committed world, rebuilt from the event log (M2 authority)."""
    db: WorldDB = request.app.state.db
    world = db.load_world()
    if world is None:
        return {"world": None, "events": 0, "last_snapshot": None}
    last = db.last_snapshot()
    return {
        "world": world.to_player_dict(),
        "events": db.event_count(),
        "last_snapshot": {"turn": last[0], "hash": last[1]} if last else None,
    }


@app.get("/api/dump-context")
async def dump_context_endpoint(request: Request) -> PlainTextResponse:
    """The metered context dump (stable head + volatile tail), for debugging."""
    if not _debug_enabled():
        raise HTTPException(status_code=404, detail="not found")
    world = _live_world(request)
    if world.is_empty:
        raise HTTPException(
            status_code=404,
            detail="no world yet (start a session: POST /api/new)",
        )
    db: WorldDB = request.app.state.db
    return PlainTextResponse(dump_context(world, messages=db.messages()))
