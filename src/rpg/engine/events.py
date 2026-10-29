"""Event log (roadmap M2): append-only events, per-turn snapshots, SQLite.

- Every engine change is an event; the log is the single source of truth.
- `snapshot` names each end-of-turn world hash (undo/resume, M6 decision).
- `rebuild_world(events) -> WorldState` must be an exact inverse of the live
  writes — pinned by a property test (M2 acceptance criterion).

Tables (the state/memory layout): pc, npc, item, quest, location, fact, event
(append-only), snapshot, message (user line + final narration only — never
tool chatter). M5 adds the rolling-summary table (single row, like `spine`)
and the `fact_fts` FTS5 index over facts (recall, the state/memory layout). The entity
tables are a materialized view: on load the world is rebuilt from the event
log and verified against the last snapshot, then the tables are resynced from
the authoritative replay.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rpg.dm.spine import StorySpine
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
        # story spine (M3: set once at session start, then clamped deltas only)
        "spine_set",
        "spine_delta",
        # rolling campaign summary (M5: folded by the memory step, replayed verbatim)
        "summary_set",
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
CREATE TABLE IF NOT EXISTS spine (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  spine TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fact (
  id INTEGER PRIMARY KEY,
  subject TEXT NOT NULL,
  predicate TEXT NOT NULL,
  value TEXT NOT NULL,
  turn INTEGER NOT NULL,
  source TEXT NOT NULL DEFAULT ''
);
CREATE VIRTUAL TABLE IF NOT EXISTS fact_fts USING fts5(
  subject, predicate, value, turn UNINDEXED
);
CREATE TABLE IF NOT EXISTS summary (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  summary TEXT NOT NULL,
  watermark INTEGER NOT NULL DEFAULT 0
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


_FTS_STOPWORDS = frozenset(
    "a an and are as at be but by do for from had has have if in is it its "
    "i me my no not of on or so that the their them they this to was we were "
    "what when where which who will with you your about into just".split()
)


def _fts_terms(query: str) -> list[str]:
    """Reduce a free-text query to FTS5 terms (lowercased, deduped, stopword-free).

    Words under three letters and English function words are dropped; the rest
    are what `recall_facts` matches against the fact index.
    """
    terms: list[str] = []
    for word in re.findall(r"[a-z0-9]+", query.casefold()):
        if len(word) < 3 or word in _FTS_STOPWORDS:
            continue
        if word not in terms:
            terms.append(word)
    return terms


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

    def recall_facts(self, query: str, limit: int = 5) -> list[Fact]:
        """FTS5 recall of the facts relevant to a query (milestone M5, state/memory layout).

        No embeddings (a solo MVP): the query is reduced to FTS5 terms and
        matched against the `fact_fts` index; rows come back ranked (bm25)
        and are deduped by (subject, predicate, value). An unmatchable query
        (no terms) recalls nothing.
        """
        terms = _fts_terms(query)
        if not terms:
            return []
        match = " OR ".join(f'"{term}"' for term in terms)
        try:
            rows = self._conn.execute(
                "SELECT subject, predicate, value, turn FROM fact_fts "
                "WHERE fact_fts MATCH ? ORDER BY rank LIMIT ?",
                (match, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        facts: list[Fact] = []
        seen: set[tuple[str, str, str]] = set()
        for subject, predicate, value, turn in rows:
            key = (subject.casefold(), predicate.casefold(), value.casefold())
            if key in seen:
                continue
            seen.add(key)
            facts.append(Fact(subject=subject, predicate=predicate, value=value, turn=turn))
        return facts

    def backup_to(self, path: str | Path) -> None:
        """Copy this database to `path` (SQLite online backup; safe while open).

        A checkpoint the player can later resume with `/load` (roadmap M5):
        the copy is a full, self-contained world (log + tables + snapshots).
        """
        dest = Path(path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest_conn = sqlite3.connect(dest)
        try:
            self._conn.backup(dest_conn)
        finally:
            dest_conn.close()

    def restore_from(self, path: str | Path) -> None:
        """Replace this database's contents with `path` (the `/load` swap).

        The SQLite online-backup API in reverse: the save file's pages are
        copied over the live connection, which then reads the save's log.
        Call `load_world` afterwards — the in-memory world is stale.
        """
        src_conn = sqlite3.connect(Path(path))
        try:
            src_conn.backup(self._conn)
        finally:
            src_conn.close()

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
                    f"world at turn {last[0]} does not match its snapshot: "
                    f"{actual} != {last[1]} — either the event log is corrupted, "
                    "or this database was written by a previous engine version "
                    "(a stale dev volume: Docker volumes survive `docker system "
                    "prune` without -v). Do not play on it — delete the "
                    "database (or the RPG_DATA volume) and start a new session."
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
        row = self._conn.execute("SELECT spine FROM spine WHERE id = 1").fetchone()
        if row is not None:
            world.spine = StorySpine.from_dict(json.loads(row[0]))
        row = self._conn.execute("SELECT summary, watermark FROM summary WHERE id = 1").fetchone()
        if row is not None:
            world.summary = row[0]
            world.summary_watermark = row[1]
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
        self._conn.execute("DELETE FROM spine")
        if world.spine is not None:
            self._conn.execute(
                "INSERT INTO spine (id, spine) VALUES (1, ?)",
                (json.dumps(world.spine.to_dict(), sort_keys=True),),
            )
        self._conn.execute("DELETE FROM summary")
        if world.summary:
            self._conn.execute(
                "INSERT INTO summary (id, summary, watermark) VALUES (1, ?, ?)",
                (world.summary, world.summary_watermark),
            )
        self._conn.execute("DELETE FROM fact_fts")
        for fact in world.facts:
            self._conn.execute(
                "INSERT INTO fact_fts (subject, predicate, value, turn) VALUES (?, ?, ?, ?)",
                (fact.subject, fact.predicate, fact.value, fact.turn),
            )
