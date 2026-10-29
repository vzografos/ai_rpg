"""Sessions (roadmap M5): /new /save /load, multi-session, restart resume.

Model: one live database (`$RPG_DATA/rpg.db`, a Docker volume — `docker
restart` resumes it) plus named saves (`$RPG_DATA/saves/<name>.db`). A save
is a full, self-contained copy (log + tables + snapshots) via the SQLite
online-backup API, so each save is an independently resumable campaign:
multi-session is "several named saves," and `/load` is "swap the live DB for
a save." `/new` seeds the pre-authored adventure only into an empty live DB.

The arbiter's meta verbs (`/new /save /load`) and the web's session
endpoints both run through this module, so the save naming and the swap are
defined once.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from rpg.dm.spine import adventure_init_payload, load_adventure
from rpg.engine.events import Message, WorldDB, WorldEvent
from rpg.engine.state import WorldState

__all__ = [
    "SAVE_NAME_RE",
    "ADVENTURE_PATH",
    "saves_dir",
    "save_name",
    "save_path",
    "list_saves",
    "save_session",
    "load_session",
    "seed_adventure",
]

ADVENTURE_PATH = Path(__file__).parent / "content" / "shadow_crown.json"
SAVE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")


def saves_dir() -> Path:
    """Where named saves live: `$RPG_DATA/saves` (next to the live db)."""
    return Path(os.environ.get("RPG_DATA", "data")) / "saves"


def save_name(name: str | None, turn: int) -> str:
    """Validate a save name (default: `turn-<N>`); a ValueError names the rule."""
    resolved = (name or f"turn-{turn}").strip()
    if not SAVE_NAME_RE.match(resolved):
        raise ValueError(
            f"invalid save name {resolved!r}: start with a letter or digit, "
            "then letters/digits/'-'/'_' (max 40)"
        )
    return resolved


def save_path(name: str) -> Path:
    return saves_dir() / f"{name}.db"


def list_saves() -> list[str]:
    """The named saves, oldest filename first ("" when the dir is absent)."""
    directory = saves_dir()
    if not directory.exists():
        return []
    return sorted(path.stem for path in directory.glob("*.db"))


def save_session(db: WorldDB, world: WorldState, name: str | None = None) -> str:
    """`/save [name]`: checkpoint the live DB. Returns the save name."""
    resolved = save_name(name, world.turn)
    db.backup_to(save_path(resolved))
    return resolved


def load_session(db: WorldDB, name: str) -> WorldState:
    """`/load <name>`: swap the live DB for the save. Returns the new world.

    The swap is the online-backup API in reverse; the in-memory world is
    stale afterwards, so the caller rebinds from the returned world (and the
    pipeline's engine).
    """
    resolved = name.strip()
    if not SAVE_NAME_RE.match(resolved):
        raise ValueError(f"invalid save name: {resolved!r}")
    path = save_path(resolved)
    if not path.exists():
        saves = ", ".join(list_saves()) or "none"
        raise ValueError(f"no such save: {resolved!r} (available: {saves})")
    db.restore_from(path)
    world = db.load_world()
    if world is None:
        raise ValueError(f"save {resolved!r} is empty")
    return world


def seed_adventure(
    db: WorldDB, pc_name: str | None = None, model: str = ""
) -> tuple[WorldState, str]:
    """`/new`: seed the pre-authored adventure + spine into an empty live DB.

    Refuses a non-empty DB (the caller offers `/save` first). Returns the
    freshly committed world (turn 1) and its opening narration.
    """
    if not db.is_empty():
        raise ValueError(
            f"a session is already running ({db.event_count()} events) — save it first"
        )
    adventure = load_adventure(ADVENTURE_PATH)
    payload = adventure_init_payload(adventure, pc_name=pc_name)
    spine_payload: dict[str, Any] = {"spine": adventure.spine.to_dict()}
    world = WorldState()
    turn = world.turn
    world.apply_event("world_init", payload, turn)
    world.apply_event("spine_set", spine_payload, turn)
    suffix = f" (model {model})." if model else "."
    narration = f"Session started: {adventure.title}{suffix}"
    db.commit(
        world,
        [
            WorldEvent(turn=turn, kind="world_init", payload=payload),
            WorldEvent(turn=turn, kind="spine_set", payload=spine_payload),
        ],
        [
            Message(turn=turn, role="user", content=f"/new {pc_name or adventure.pc['name']}"),
            Message(turn=turn, role="narration", content=narration),
        ],
    )
    return world, narration
