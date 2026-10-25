"""World state (roadmap M2): entities in the SQLite-backed world.

Entities (the state/memory layout): pc, npc, item, quest, location, fact.
`WorldState` is the live, queryable view; the append-only event log
(events.py) is the authority and `rebuild_world(events)` must reproduce it
exactly (property test, M2).
"""
