"""WorldDB tests (M2): append-only log, per-turn snapshots, kill + restart.

Pins the M2 acceptance: the event log is the authority — a killed process
reopens the same file and rebuilds a byte-identical world, verified against
the last committed snapshot.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from rpg.dm.spine import StorySpine
from rpg.engine.events import (
    EVENT_KINDS,
    MESSAGE_ROLES,
    Message,
    WorldCorruptionError,
    WorldDB,
    WorldEvent,
    default_db_path,
    rebuild_world,
)
from rpg.engine.state import WorldState

INITIAL = {
    "pc": {
        "name": "Mira",
        "hp": 12,
        "max_hp": 12,
        "ac": 14,
        "abilities": {"strength": 10},
        "inventory": {},
        "gold": 5,
        "xp": 0,
    },
    "pc_location": "Square",
    "locations": [{"name": "Square", "description": "", "exits": []}],
    "npcs": [{"name": "Brenna", "location": "Square", "agenda": "keeper-only secret"}],
    "quests": [],
    "items": [],
}


def fresh_world() -> WorldState:
    world = WorldState()
    world.apply_event("world_init", INITIAL, turn=0)
    return world


def commit_turn(
    db: WorldDB, world: WorldState, events: list[WorldEvent], text: str, narration: str
) -> None:
    """Apply the turn's events to the live world, then commit (the contract)."""
    turn = world.turn
    for event in events:
        world.apply_event(event.kind, event.payload, event.turn)
    db.commit(
        world,
        events,
        [
            Message(turn=turn, role="user", content=text),
            Message(turn=turn, role="narration", content=narration),
        ],
    )


# --- event/message validation -------------------------------------------------------


def test_world_event_validates_kind_and_payload():
    with pytest.raises(ValueError, match="unknown event kind"):
        WorldEvent(turn=0, kind="make_it_rain", payload={})
    with pytest.raises(ValueError, match="payload"):
        WorldEvent(turn=0, kind="pc_damage", payload="not an object")
    with pytest.raises(ValueError, match="turn"):
        WorldEvent(turn=-1, kind="pc_damage", payload={})
    with pytest.raises(ValueError, match="seq"):
        WorldEvent(turn=0, kind="pc_damage", payload={}, seq=0)
    assert set(EVENT_KINDS) >= {"world_init", "pc_damage", "combat_begin", "fact_add"}


def test_message_validates_role_and_content():
    assert set(MESSAGE_ROLES) == {"user", "narration"}
    with pytest.raises(ValueError, match="role"):
        Message(turn=0, role="tool", content="x")
    with pytest.raises(ValueError, match="content"):
        Message(turn=0, role="user", content="")
    with pytest.raises(ValueError, match="id"):
        Message(turn=0, role="user", content="x", id=0)
    with pytest.raises(ValueError, match="turn"):
        Message(turn=-1, role="user", content="x")


def test_default_db_path_uses_the_configured_data_directory(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RPG_DATA", "/tmp/rpg-data-test")
    assert default_db_path() == Path("/tmp/rpg-data-test/rpg.db")


def test_rebuild_world_replays_in_order_and_rejects_scrambled_seqs():
    events = [
        WorldEvent(turn=0, kind="world_init", payload=INITIAL, seq=1),
        WorldEvent(turn=1, kind="pc_damage", payload={"amount": 2}, seq=2),
    ]
    world = rebuild_world(events)
    assert world.pc.sheet.hp == 10
    # A log whose first entry does not start at seq 1 is corrupted...
    with pytest.raises(ValueError, match="start at seq 1"):
        rebuild_world([WorldEvent(turn=1, kind="pc_damage", payload={"amount": 1}, seq=2)])
    # ...as is a scrambled one (a repeated seq).
    with pytest.raises(ValueError, match="out of order"):
        rebuild_world(
            [
                WorldEvent(turn=0, kind="world_init", payload=INITIAL, seq=1),
                WorldEvent(turn=1, kind="pc_damage", payload={"amount": 1}, seq=1),
            ]
        )
    # A log without world_init cannot apply pc events.
    with pytest.raises(ValueError, match="no player character"):
        rebuild_world([WorldEvent(turn=0, kind="pc_damage", payload={"amount": 1}, seq=1)])


# --- the log: append-only, ordered, per-turn snapshots --------------------------------


def test_commit_appends_events_and_names_the_turn(tmp_path: Path):
    with WorldDB(tmp_path / "w.db") as db:
        world = fresh_world()
        db.commit(world, [WorldEvent(turn=0, kind="world_init", payload=INITIAL)], [])
        assert world.turn == 1  # the counter advances with the transaction
        commit_turn(
            db,
            world,
            [WorldEvent(turn=1, kind="pc_damage", payload={"amount": 2})],
            "I fight",
            "You hit.",
        )
        events = db.events()
    assert [e.seq for e in events] == [1, 2]
    assert [e.turn for e in events] == [0, 1]
    assert [e.kind for e in events] == ["world_init", "pc_damage"]
    assert events[1].payload == {"amount": 2}  # payload round-trips as JSON


def test_commit_rejects_mismatched_turns_and_unknown_kinds(tmp_path: Path):
    with WorldDB(tmp_path / "w.db") as db:
        world = fresh_world()
        db.commit(world, [WorldEvent(turn=0, kind="world_init", payload=INITIAL)], [])
        with pytest.raises(ValueError, match="event turn"):
            db.commit(world, [WorldEvent(turn=5, kind="pc_damage", payload={"amount": 1})], [])
        with pytest.raises(ValueError, match="unknown event kind"):
            db.commit(world, [WorldEvent(turn=1, kind="make_it_rain", payload={})], [])
        with pytest.raises(ValueError, match="message turn"):
            db.commit(world, [], [Message(turn=9, role="user", content="x")])
    # A rejected commit leaves the log untouched.
    with WorldDB(tmp_path / "w.db") as db:
        assert db.event_count() == 1
        assert db.messages() == []


def test_commit_rechecks_a_tampered_event_kind(tmp_path: Path):
    with WorldDB(tmp_path / "w.db") as db:
        world = fresh_world()
        event = WorldEvent(turn=0, kind="world_init", payload=INITIAL)
        # WorldEvent is frozen, but commit still defends its own boundary if
        # an object was tampered with after construction.
        object.__setattr__(event, "kind", "make_it_rain")
        with pytest.raises(ValueError, match="unknown event kind"):
            db.commit(world, [event], [])
        assert db.event_count() == 0


def test_commit_rolls_back_log_and_snapshot_when_table_sync_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    with WorldDB(tmp_path / "w.db") as db:
        world = fresh_world()

        def fail_resync(_world: WorldState) -> None:
            raise RuntimeError("simulated table sync failure")

        monkeypatch.setattr(db, "_resync_tables", fail_resync)
        with pytest.raises(RuntimeError, match="table sync"):
            db.commit(world, [WorldEvent(turn=0, kind="world_init", payload=INITIAL)], [])

        assert db.event_count() == 0
        assert db.last_snapshot() is None
        assert not db._conn.in_transaction
        assert world.turn == 0


def test_event_log_has_no_amend_surface(tmp_path: Path):
    # The public surface exposes no way to amend, delete, or reorder events.
    mutating = [
        name
        for name in dir(WorldDB)
        if any(word in name for word in ("delete", "update", "amend", "rewrite", "truncate"))
    ]
    assert mutating == []
    with WorldDB(tmp_path / "w.db") as db:
        world = fresh_world()
        db.commit(world, [WorldEvent(turn=0, kind="world_init", payload=INITIAL)], [])
        assert db.events()  # readable
    # The schema itself never UPDATEs or DELETEs the event table:
    with WorldDB(tmp_path / "w.db") as db:
        assert db.event_count() == 1


def test_messages_are_ordered_and_role_checked(tmp_path: Path):
    with WorldDB(tmp_path / "w.db") as db:
        world = fresh_world()
        db.commit(world, [WorldEvent(turn=0, kind="world_init", payload=INITIAL)], [])
        commit_turn(db, world, [], "line one", "reply one")
        commit_turn(db, world, [], "line two", "reply two")
        messages = db.messages()
        assert [(m.role, m.content) for m in messages] == [
            ("user", "line one"),
            ("narration", "reply one"),
            ("user", "line two"),
            ("narration", "reply two"),
        ]
        assert [m.id for m in messages] == [1, 2, 3, 4]
        # The schema rejects tool chatter (the state/memory layout: user line + narration only).
        with pytest.raises(sqlite3.IntegrityError):
            db._conn.execute("INSERT INTO message (turn, role, content) VALUES (0, 'tool', 'x')")


def test_snapshots_name_every_committed_turn(tmp_path: Path):
    with WorldDB(tmp_path / "w.db") as db:
        world = fresh_world()
        db.commit(world, [WorldEvent(turn=0, kind="world_init", payload=INITIAL)], [])
        snapshot_at_0 = db.last_snapshot()
        commit_turn(
            db,
            world,
            [WorldEvent(turn=1, kind="pc_gain_xp", payload={"amount": 300})],
            "xp",
            "well earned.",
        )
        snapshot_at_1 = db.last_snapshot()
    assert snapshot_at_0 is not None and snapshot_at_0[0] == 0
    assert snapshot_at_1 is not None and snapshot_at_1[0] == 1
    with WorldDB(tmp_path / "w.db") as db:
        assert db.snapshot_for(0) == snapshot_at_0[1]
        assert db.snapshot_for(1) == snapshot_at_1[1]
        assert db.snapshot_for(2) is None
        # The snapshot names the world AT that turn: replay with the counter
        # set back to the snapshot's turn matches exactly, and not after it.
        rebuilt = rebuild_world(db.events())
        rebuilt.turn = 1
        assert rebuilt.fingerprint() == snapshot_at_1[1]
        rebuilt.turn = 2
        assert rebuilt.fingerprint() != snapshot_at_1[1]


# --- load: rebuild from the log, verify, resync -----------------------------------------


def test_load_world_from_an_empty_db_is_none(tmp_path: Path):
    with WorldDB(tmp_path / "w.db") as db:
        assert db.is_empty()
        assert db.load_world() is None
        assert db.load_from_tables() is None


def test_kill_and_restart_rebuilds_an_identical_world(tmp_path: Path):
    db_path = tmp_path / "w.db"
    live_hash = None
    with WorldDB(db_path) as db:  # the original process
        world = fresh_world()
        db.commit(world, [WorldEvent(turn=0, kind="world_init", payload=INITIAL)], [])
        commit_turn(
            db,
            world,
            [WorldEvent(turn=1, kind="pc_damage", payload={"amount": 3})],
            "I brawl",
            "You take 3.",
        )
        commit_turn(
            db,
            world,
            [
                WorldEvent(
                    turn=2,
                    kind="fact_add",
                    payload={"subject": "Brenna", "predicate": "saw", "value": "blood"},
                )
            ],
            "I watch",
            "You watch.",
        )
        live_hash = world.fingerprint()
        # "kill": the connection closes with the context exit — no further writes.
    with WorldDB(db_path) as db:  # the restarted process
        world = db.load_world()
    assert world is not None
    assert world.turn == 3
    assert world.fingerprint() == live_hash
    assert world.pc.sheet.hp == 9
    assert [f.value for f in world.facts] == ["blood"]
    with WorldDB(db_path) as db:
        assert [(m.role, m.content) for m in db.messages()] == [
            ("user", "I brawl"),
            ("narration", "You take 3."),
            ("user", "I watch"),
            ("narration", "You watch."),
        ]


def test_load_world_resyncs_corrupted_entity_tables_from_the_log(tmp_path: Path):
    db_path = tmp_path / "w.db"
    with WorldDB(db_path) as db:
        world = fresh_world()
        db.commit(world, [WorldEvent(turn=0, kind="world_init", payload=INITIAL)], [])
        commit_turn(
            db, world, [WorldEvent(turn=1, kind="pc_gold", payload={"amount": 4})], "gold", "found."
        )
        good = world.fingerprint()
    # Corrupt the materialized table (a torn write): the log must heal it.
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE pc SET gold = 999")
    conn.commit()
    conn.close()
    with WorldDB(db_path) as db:
        assert db.load_from_tables().pc.sheet.gold == 999  # the corrupted view
        world = db.load_world()  # authoritative replay + resync
        assert world is not None and world.pc.sheet.gold == 9
        assert world.fingerprint() == good
        assert db.load_from_tables().pc.sheet.gold == 9  # healed


def test_load_from_tables_restores_a_materialized_story_spine(tmp_path: Path):
    world = fresh_world()
    story = StorySpine.from_dict(
        {
            "premise": "A crown must stay lost.",
            "stakes": "The village is at risk.",
            "antagonist": "Vesper",
            "beats": [{"text": "Find the candle", "act": 1}],
            "finale": "The harvest is saved.",
        }
    )
    payload = {"spine": story.to_dict()}
    world.apply_event("spine_set", payload, turn=0)
    with WorldDB(tmp_path / "w.db") as db:
        db.commit(
            world,
            [
                WorldEvent(turn=0, kind="world_init", payload=INITIAL),
                WorldEvent(turn=0, kind="spine_set", payload=payload),
            ],
            [],
        )
        loaded = db.load_from_tables()

    assert loaded is not None and loaded.spine is not None
    assert loaded.spine.to_dict() == story.to_dict()


def test_spell_slot_events_replay_and_match_the_materialized_pc(tmp_path: Path):
    payload = {**INITIAL, "pc": {**INITIAL["pc"], "spellcasting_class": "wizard"}}
    with WorldDB(tmp_path / "caster.db") as db:
        world = WorldState()
        world.apply_event("world_init", payload, turn=0)
        db.commit(world, [WorldEvent(turn=0, kind="world_init", payload=payload)], [])
        spend = WorldEvent(turn=1, kind="pc_spell_slot_spend", payload={"level": 1})
        world.apply_event(spend.kind, spend.payload, spend.turn)
        db.commit(world, [spend], [])

        from_tables = db.load_from_tables()
        from_log = db.load_world()
        spent_state = world.to_dict()
        rest = WorldEvent(turn=2, kind="pc_rest", payload={"kind": "long"})
        world.apply_event(rest.kind, rest.payload, rest.turn)
        db.commit(world, [rest], [])
        after_rest = db.load_world()
        after_rest_tables = db.load_from_tables()

    assert from_tables is not None and from_log is not None
    assert from_tables.to_dict() == from_log.to_dict() == spent_state
    assert from_tables.pc.sheet.spellcasting_class == "wizard"
    assert from_tables.pc.sheet.expended_spell_slots == {1: 1}
    assert from_tables.pc.sheet.spell_slots == {1: 1}
    assert after_rest is not None and after_rest_tables is not None
    assert after_rest.to_dict() == after_rest_tables.to_dict() == world.to_dict()
    assert after_rest.pc.sheet.expended_spell_slots == {}
    assert after_rest.pc.sheet.spell_slots == {1: 2}


def test_open_migrates_legacy_pc_table_with_no_slot_columns(tmp_path: Path):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE pc (id INTEGER PRIMARY KEY CHECK (id = 1), name TEXT NOT NULL, "
        "hp INTEGER NOT NULL, max_hp INTEGER NOT NULL, ac INTEGER NOT NULL, "
        "abilities TEXT NOT NULL, inventory TEXT NOT NULL, gold INTEGER NOT NULL, "
        "xp INTEGER NOT NULL, location TEXT NOT NULL DEFAULT '', "
        "description TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '[]')"
    )
    conn.close()

    with WorldDB(path) as db:
        columns = {row[1] for row in db._conn.execute("PRAGMA table_info(pc)")}
        assert {"spellcasting_class", "expended_spell_slots"} <= columns


def test_fact_recall_dedupes_rows_and_survives_a_missing_fts_index(tmp_path: Path):
    with WorldDB(tmp_path / "w.db") as db:
        world = fresh_world()
        db.commit(world, [WorldEvent(turn=0, kind="world_init", payload=INITIAL)], [])
        payload = {"subject": "Tower", "predicate": "holds", "value": "ember"}
        world.apply_event("fact_add", payload, turn=1)
        world.apply_event("fact_add", payload, turn=1)
        db.commit(
            world,
            [
                WorldEvent(turn=1, kind="fact_add", payload=payload),
                WorldEvent(turn=1, kind="fact_add", payload=payload),
            ],
            [],
        )

        assert db.recall_facts("ember") == [world.facts[0]]
        db._conn.execute("DROP TABLE fact_fts")
        assert db.recall_facts("ember") == []


def test_load_world_detects_a_corrupted_log_against_the_snapshot(tmp_path: Path):
    db_path = tmp_path / "w.db"
    with WorldDB(db_path) as db:
        world = fresh_world()
        db.commit(world, [WorldEvent(turn=0, kind="world_init", payload=INITIAL)], [])
        commit_turn(
            db,
            world,
            [WorldEvent(turn=1, kind="pc_damage", payload={"amount": 3})],
            "hit",
            "you take 3.",
        )
    # Forge a later event by hand (bypassing the API) — the replay must no
    # longer match the last snapshot's hash.
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO event (turn, kind, payload) VALUES (?, ?, ?)",
        (2, "pc_damage", json.dumps({"amount": 77})),
    )
    conn.commit()
    conn.close()
    with WorldDB(db_path) as db:
        with pytest.raises(WorldCorruptionError, match="snapshot"):
            db.load_world()


def test_load_world_names_the_remedy_for_a_stale_volume(tmp_path: Path):
    db_path = tmp_path / "w.db"
    with WorldDB(db_path) as db:
        world = fresh_world()
        db.commit(world, [WorldEvent(turn=0, kind="world_init", payload=INITIAL)], [])
    # The log replays cleanly under the current code, but the stored hash is
    # what a previous engine version computed (the fingerprint input changed
    # since) — the stale dev volume `docker system prune` (without -v)
    # leaves behind. The refusal must name that case and the remedy.
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE snapshot SET world_hash = 'stale'")
    conn.commit()
    conn.close()
    with WorldDB(db_path) as db:
        with pytest.raises(WorldCorruptionError, match="previous engine version"):
            db.load_world()


def test_rebuild_world_matches_the_live_world_after_a_mix_of_events(tmp_path: Path):
    with WorldDB(tmp_path / "w.db") as db:
        world = fresh_world()
        db.commit(world, [WorldEvent(turn=0, kind="world_init", payload=INITIAL)], [])
        ops = [
            ("pc_damage", {"amount": 2}),
            ("npc_note", {"name": "Brenna", "text": "kept her counsel"}),
            ("item_add", {"name": "Rusty key", "location": "Square"}),
            ("quest_add", {"name": "Find the crown", "description": "The mayor pays."}),
            ("pc_move", {"location": "Square"}),
            ("fact_add", {"subject": "Brenna", "predicate": "hides", "value": "something"}),
        ]
        for index, (kind, payload) in enumerate(ops):
            commit_turn(
                db,
                world,
                [WorldEvent(turn=world.turn, kind=kind, payload=payload)],
                f"line {index}",
                f"reply {index}",
            )
        live = world.fingerprint()
    with WorldDB(tmp_path / "w.db") as db:
        rebuilt = rebuild_world(db.events())
        rebuilt.turn = world.turn
        assert rebuilt.fingerprint() == live
        assert rebuilt.to_dict() == world.to_dict()
