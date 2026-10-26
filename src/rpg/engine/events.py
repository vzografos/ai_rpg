"""Event log (roadmap M2): append-only events, per-turn snapshots, SQLite.

- Every engine change is an event; the log is the single source of truth.
- `snapshot` names each end-of-turn world hash (undo/resume, M6 decision).
- `rebuild_world(events) -> WorldState` must be an exact inverse of the live
  writes — pinned by a property test (M2 acceptance criterion).

Tables (the state/memory layout): pc, npc, item, quest, location, fact, event
(append-only), snapshot, message (user line + final narration only — never
tool chatter). The entity tables are a materialized view: on load the world
is rebuilt from the event log and verified against the last snapshot, then
the tables are resynced from the authoritative replay.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rpg.engine.sheet import CharacterSheet
from rpg.engine.state import (
    Fact,
    Item,
    Location,
    Npc,
    PlayerCharacter,
    Quest,
    WorldState,
)

__all__ = [
    "EVENT_KINDS",
    "MESSAGE_ROLES",
    "WorldEvent",
    "Message",
    "WorldCorruptionError",
    "rebuild_world",
    "default_db_path",
    "WorldDB",
]


def default_db_path() -> Path:
    """The in-container DB location: `$RPG_DATA/rpg.db` (roadmap C1: the
    data dir is a Docker volume — `rpg-data:/app/data` in compose)."""
    return Path(os.environ.get("RPG_DATA", "data")) / "rpg.db"

EVENT_KINDS = frozenset(
    {
        "world_init",
        # pc
        "pc_damage",
        "pc_heal",
        "pc_set_hp",
        "pc_gain_xp",
        "pc_gold",
        "pc_item_add",
        "pc_item_remove",
        "pc_move",
        "pc_note",
        # npcs
        "npc_add",
        "npc_note",
        "npc_set_status",
        "npc_set_location",
        # items
        "item_add",
        "item_set_holder",
        # quests
        "quest_add",
        "quest_set_status",
        # locations
        "location_add",
        # facts
        "fact_add",
        "fact_remove",
        # combat (authoritative committed results — never re-rolled)
        "combat_begin",
        "combat_attack",
        "combat_turn",
        "combat_condition",
        "combat_heal",
        "combat_end",
    }
)

MESSAGE_ROLES = ("user", "narration")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pc (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  name TEXT NOT NULL,
  hp INTEGER NOT NULL,
  max_hp INTEGER NOT NULL,
  ac INTEGER NOT NULL,
  abilities TEXT NOT NULL,
  inventory TEXT NOT NULL,
  gold INTEGER NOT NULL,
  xp INTEGER NOT NULL,
  location TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  notes TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS npc (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  location TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'alive',
  agenda TEXT NOT NULL DEFAULT '',
  notes TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS item (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  holder TEXT NOT NULL DEFAULT '',
  location TEXT NOT NULL DEFAULT '',
  quantity INTEGER NOT NULL DEFAULT 1,
  description TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS quest (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  description TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'open',
  reward TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS location (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  description TEXT NOT NULL DEFAULT '',
  exits TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS fact (
  id INTEGER PRIMARY KEY,
  subject TEXT NOT NULL,
  predicate TEXT NOT NULL,
  value TEXT NOT NULL,
  turn INTEGER NOT NULL,
  source TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS event (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  turn INTEGER NOT NULL,
  kind TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshot (
  turn INTEGER PRIMARY KEY,
  world_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS message (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  turn INTEGER NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('user', 'narration')),
  content TEXT NOT NULL
);
"""


class WorldCorruptionError(RuntimeError):
    """The event log no longer reproduces the committed world (snapshot mismatch)."""


@dataclass(frozen=True)
class WorldEvent:
    """One append-only log entry. `seq` is assigned by the DB on insert."""

    turn: int
    kind: str
    payload: dict[str, Any]
    seq: int | None = None

    def __post_init__(self) -> None:
        if self.seq is not None and self.seq < 1:
            raise ValueError(f"event seq must be >= 1: {self.seq}")
        if self.turn < 0:
            raise ValueError(f"event turn must be >= 0: {self.turn}")
        if self.kind not in EVENT_KINDS:
            raise ValueError(f"unknown event kind: {self.kind!r}")
        if not isinstance(self.payload, dict):
            raise ValueError(f"event payload must be an object: {type(self.payload).__name__}")


@dataclass(frozen=True)
class Message:
    """A history line: only the user line and the final narration (the state/memory layout)."""

    turn: int
    role: str
    content: str
    id: int | None = None

    def __post_init__(self) -> None:
        if self.id is not None and self.id < 1:
            raise ValueError(f"message id must be >= 1: {self.id}")
        if self.turn < 0:
            raise ValueError(f"message turn must be >= 0: {self.turn}")
        if self.role not in MESSAGE_ROLES:
            raise ValueError(f"message role must be one of {MESSAGE_ROLES!r}: {self.role!r}")
        if not self.content:
            raise ValueError("message content must be non-empty")


def rebuild_world(events: list[WorldEvent]) -> WorldState:
    """Replay the event log from scratch — the M2 property test's other side.

    The result's `turn` counter is 0 (the session concept the DB assigns);
    `WorldDB.load_world` restores it from the snapshot count.
    """
    previous_seq = 0
    world = WorldState()
    for event in events:
        if event.seq is not None:
            if previous_seq == 0 and event.seq != 1:
                raise ValueError(f"event log must start at seq 1, got {event.seq}")
            if event.seq <= previous_seq:
                raise ValueError(f"event log out of order at seq {event.seq}")
            previous_seq = event.seq
        world.apply_event(event.kind, event.payload, event.turn)
    return world


class WorldDB:
    """SQLite-backed world: the append-only log plus the materialized tables.

    `commit(world, events, messages)` is one transaction: append the
    turn's events, resync the entity tables, name the end-of-turn world in
    the snapshot, and append the turn's messages. Nothing else writes to
    `event` — the log is append-only by construction.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, isolation_level=None)
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> WorldDB:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- the log (read-only surface; nothing here can amend or delete) ---------

    def is_empty(self) -> bool:
        return self._conn.execute("SELECT COUNT(*) FROM event").fetchone()[0] == 0

    def event_count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM event").fetchone()[0]

    def events(self, since_seq: int = 0) -> list[WorldEvent]:
        rows = self._conn.execute(
            "SELECT seq, turn, kind, payload FROM event WHERE seq > ? ORDER BY seq", (since_seq,)
        ).fetchall()
        return [
            WorldEvent(turn=turn, kind=kind, payload=json.loads(payload), seq=seq)
            for seq, turn, kind, payload in rows
        ]

    def last_snapshot(self) -> tuple[int, str] | None:
        sql = "SELECT turn, world_hash FROM snapshot ORDER BY turn DESC LIMIT 1"
        row = self._conn.execute(sql).fetchone()
        return (row[0], row[1]) if row else None

    def snapshot_for(self, turn: int) -> str | None:
        sql = "SELECT world_hash FROM snapshot WHERE turn = ?"
        row = self._conn.execute(sql, (turn,)).fetchone()
        return row[0] if row else None

    def messages(self, since_id: int = 0) -> list[Message]:
        rows = self._conn.execute(
            "SELECT id, turn, role, content FROM message WHERE id > ? ORDER BY id", (since_id,)
        ).fetchall()
        return [
            Message(turn=turn, role=role, content=content, id=message_id)
            for message_id, turn, role, content in rows
        ]

    # -- load: rebuild from the log (authoritative), verify, resync tables ----

    def load_world(self) -> WorldState | None:
        """Rebuild the world from the event log alone.

        Verifies the replay against the last committed snapshot, restores
        the turn counter (one snapshot row per committed turn), and resyncs
        the materialized entity tables from the authoritative replay.
        Returns None for a DB with no events.
        """
        events = self.events()
        if not events:
            return None
        world = rebuild_world(events)
        world.turn = self._conn.execute("SELECT COUNT(*) FROM snapshot").fetchone()[0]
        last = self.last_snapshot()
        if last is not None:
            saved_turn = world.turn
            world.turn = last[0]
            try:
                actual = world.fingerprint()
            finally:
                world.turn = saved_turn
            if actual != last[1]:
                raise WorldCorruptionError(
                    f"world at turn {last[0]} does not match its snapshot: {actual} != {last[1]}"
                )
        self._resync_tables(world)
        return world

    def load_from_tables(self) -> WorldState | None:
        """The materialized entity tables as a world (second view, for tests).

        Reads only pc/npc/item/quest/location/fact — the exact opposite
        code path of `load_world` (event replay). The M2 property test
        asserts the two views agree.
        """
        if self._conn.execute("SELECT COUNT(*) FROM event").fetchone()[0] == 0:
            return None
        world = WorldState()
        row = self._conn.execute(
            "SELECT name, hp, max_hp, ac, abilities, inventory, gold, xp, location, "
            "description, notes FROM pc"
        ).fetchone()
        if row is not None:
            world.pc = PlayerCharacter(
                sheet=CharacterSheet(
                    name=row[0], hp=row[1], max_hp=row[2], ac=row[3],
                    abilities=json.loads(row[4]), inventory=json.loads(row[5]),
                    gold=row[6], xp=row[7],
                ),
                location=row[8],
                description=row[9],
                notes=json.loads(row[10]),
            )
        for name, location, description, status, agenda, notes in self._conn.execute(
            "SELECT name, location, description, status, agenda, notes FROM npc ORDER BY name"
        ):
            world.npcs[name] = Npc(
                name=name, location=location, description=description,
                status=status, agenda=agenda, notes=json.loads(notes),
            )
        for name, holder, location, quantity, description in self._conn.execute(
            "SELECT name, holder, location, quantity, description FROM item ORDER BY name"
        ):
            world.items[name] = Item(
                name=name, holder=holder, location=location,
                quantity=quantity, description=description,
            )
        for name, description, status, reward in self._conn.execute(
            "SELECT name, description, status, reward FROM quest ORDER BY name"
        ):
            world.quests[name] = Quest(
                name=name, description=description, status=status, reward=reward
            )
        for name, description, exits in self._conn.execute(
            "SELECT name, description, exits FROM location ORDER BY name"
        ):
            world.locations[name] = Location(
                name=name, description=description, exits=json.loads(exits)
            )
        for subject, predicate, value, turn, source in self._conn.execute(
            "SELECT subject, predicate, value, turn, source FROM fact ORDER BY id"
        ):
            world.facts.append(
                Fact(subject=subject, predicate=predicate, value=value, turn=turn, source=source)
            )
        world.turn = self._conn.execute("SELECT COUNT(*) FROM snapshot").fetchone()[0]
        return world

    # -- commit: one transaction per turn ---------------------------------------

    def commit(self, world: WorldState, events: list[WorldEvent], messages: list[Message]) -> None:
        """Commit one turn: log events, resync tables, snapshot, history.

        The caller has already applied `events` to `world` (apply_event is
        the only writer). All events/messages must carry the turn being
        committed (`world.turn`); the snapshot names the world at that
        turn, and the counter advances only with the transaction.
        """
        for event in events:
            if event.turn != world.turn:
                raise ValueError(f"event turn {event.turn} != committed turn {world.turn}")
            if event.kind not in EVENT_KINDS:
                raise ValueError(f"unknown event kind: {event.kind!r}")
        for message in messages:
            if message.turn != world.turn:
                raise ValueError(f"message turn {message.turn} != committed turn {world.turn}")
        world_hash = world.fingerprint()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            for event in events:
                payload_json = json.dumps(event.payload, sort_keys=True, separators=(",", ":"))
                self._conn.execute(
                    "INSERT INTO event (turn, kind, payload) VALUES (?, ?, ?)",
                    (event.turn, event.kind, payload_json),
                )
            self._resync_tables(world)
            self._conn.execute(
                "INSERT INTO snapshot (turn, world_hash) VALUES (?, ?)", (world.turn, world_hash)
            )
            for message in messages:
                self._conn.execute(
                    "INSERT INTO message (turn, role, content) VALUES (?, ?, ?)",
                    (message.turn, message.role, message.content),
                )
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")
        world.turn += 1

    # -- internals ---------------------------------------------------------------

    def _resync_tables(self, world: WorldState) -> None:
        """Replace the materialized entity tables from a world (cache resync)."""
        self._conn.execute("DELETE FROM pc")
        if world.pc is not None:
            sheet = world.pc.sheet
            self._conn.execute(
                "INSERT INTO pc (id, name, hp, max_hp, ac, abilities, inventory, "
                "gold, xp, location, description, notes) "
                "VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    sheet.name, sheet.hp, sheet.max_hp, sheet.ac,
                    json.dumps(dict(sheet.abilities), sort_keys=True),
                    json.dumps(dict(sheet.inventory), sort_keys=True),
                    sheet.gold, sheet.xp,
                    world.pc.location, world.pc.description, json.dumps(world.pc.notes),
                ),
            )
        self._conn.execute("DELETE FROM npc")
        for npc in world.npcs.values():
            self._conn.execute(
                "INSERT INTO npc (name, location, description, status, agenda, notes) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    npc.name,
                    npc.location,
                    npc.description,
                    npc.status,
                    npc.agenda,
                    json.dumps(npc.notes),
                ),
            )
        self._conn.execute("DELETE FROM item")
        for item in world.items.values():
            self._conn.execute(
                "INSERT INTO item (name, holder, location, quantity, description) "
                "VALUES (?, ?, ?, ?, ?)",
                (item.name, item.holder, item.location, item.quantity, item.description),
            )
        self._conn.execute("DELETE FROM quest")
        for quest in world.quests.values():
            self._conn.execute(
                "INSERT INTO quest (name, description, status, reward) VALUES (?, ?, ?, ?)",
                (quest.name, quest.description, quest.status, quest.reward),
            )
        self._conn.execute("DELETE FROM location")
        for location in world.locations.values():
            self._conn.execute(
                "INSERT INTO location (name, description, exits) VALUES (?, ?, ?)",
                (location.name, location.description, json.dumps(list(location.exits))),
            )
        self._conn.execute("DELETE FROM fact")
        for fact in world.facts:
            self._conn.execute(
                "INSERT INTO fact (subject, predicate, value, turn, source) VALUES (?, ?, ?, ?, ?)",
                (fact.subject, fact.predicate, fact.value, fact.turn, fact.source),
            )
