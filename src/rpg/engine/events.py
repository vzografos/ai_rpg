"""Event log (roadmap M2): append-only events, per-turn snapshots.

- Every engine change is an event; the log is the single source of truth.
- `snapshot` names each end-of-turn world hash (undo/resume, M6 decision).
- `rebuild_world(events) -> WorldState` must be an exact inverse of the live
  writes — pinned by a property test (M2 acceptance criterion).

Schema arrives with the SQLite tables in M2 (pc, npc, item, quest,
location, fact, event, snapshot, message).
"""
