"""FastAPI app: the transport-agnostic game server (the documented layout, constraint C2).

M2 surface:
    GET  /                   the single static page (chat pane + sidebar, vanilla JS)
    GET  /api/status         app + model info
    GET  /api/ping           one-shot LLM round-trip ("LLM says hello")
    POST /action             player input -> SSE stream (M3: the turn pipeline)
    GET  /api/world          the committed world (rebuilt from the event log)
    GET  /api/dump-context   the metered context dump (debug)

M3 wires the real turn pipeline behind POST /action (dm/loop.py) and adds
dice frames, state-update frames, /api/roll, /api/save|/load. The browser,
the in-container debug CLI, and the test harness consume the same frames —
the pipeline never learns which client it serves.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

from rpg import __version__
from rpg.engine.context import dump_context
from rpg.engine.events import WorldDB, default_db_path
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.provider = build_provider()
    app.state.db = WorldDB(default_db_path())
    app.state.world = app.state.db.load_world()  # the M3 turn pipeline's live world
    yield
    app.state.db.close()
    await app.state.provider.aclose()


app = FastAPI(title="ai_rpg", version=__version__, lifespan=lifespan)


class ActionIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/status")
async def status(request: Request) -> dict[str, Any]:
    provider: LLMProvider = request.app.state.provider
    return {
        "app": "ai_rpg",
        "version": __version__,
        "milestone": "M2",
        "model": provider.config.model,
        "base_url": provider.config.base_url,
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


@app.post("/action")
async def action(body: ActionIn, request: Request) -> StreamingResponse:
    """M0 one-shot completion streamed as narration deltas (SSE).

    M3: the 9-step turn pipeline — arbiter, intent, context, DM round loop,
    adjudication, backstops, commit, memory maintenance, render. Same
    endpoint, same frame grammar; the body grows (dice frames, state frames).
    """
    provider: LLMProvider = request.app.state.provider
    messages = prompts.action_messages(body.text)

    async def frames() -> AsyncIterator[str]:
        try:
            reply = await provider.chat(messages, temperature=0.9)
        except ProviderError as exc:
            yield sse_frame("error", str(exc))
            return
        # M0 streams the whole completion in one delta; M3 yields live deltas
        # as the model generates and interleaves engine frames.
        if reply.content:
            yield sse_frame("narration", reply.content)
        for call in reply.tool_calls:
            yield sse_frame("tool", {"name": call.name, "arguments": call.arguments})
        yield sse_frame("done", {"usage": reply.usage.total_tokens, "model": provider.config.model})

    return StreamingResponse(
        frames(),
        media_type=SSE_MEDIA_TYPE,
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/world")
async def world_status(request: Request) -> dict[str, Any]:
    """The committed world, rebuilt from the event log (the M2 authority)."""
    db: WorldDB = request.app.state.db
    world = db.load_world()
    if world is None:
        return {"world": None, "events": 0, "last_snapshot": None}
    last = db.last_snapshot()
    return {
        "world": world.to_dict(),
        "events": db.event_count(),
        "last_snapshot": {"turn": last[0], "hash": last[1]} if last else None,
    }


@app.get("/api/dump-context")
async def dump_context_endpoint(request: Request) -> PlainTextResponse:
    """The metered context dump (stable head + volatile tail), for debugging."""
    db: WorldDB = request.app.state.db
    world = db.load_world()
    if world is None:
        raise HTTPException(
            status_code=404,
            detail="no world yet (create one with the CLI: rpg.cli new)",
        )
    return PlainTextResponse(dump_context(world, messages=db.messages()))
