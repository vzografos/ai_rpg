"""Story spine (the story spine, M3): the hidden scaffold that keeps the story honest.

Shape (ODM's, simplified):
    StorySpine {
      premise, stakes, antagonist,
      beats: [{text, status: pending|active|done|skipped, act,
               waypoints: [{kind: place|npc|item|objective|fight|narrative, text, done}],
               detail: ""}],
      cast:  [{name, role, agenda (DM-only), status}],
      events: [{kind, name, detail (DM-only), trigger, status}],
      finale: str
    }

Rules:
- generated at session start (or from a pre-authored adventure JSON),
  schema-validated before use
- clamped refresh at beat completion: may mark done/skipped, append
  annotations, add sub-threads; may never rewrite completed beats
- waypoints are ticked by the SERVER from the DM's own tool calls — the
  story advances through real play, not model whim
- player-safe projection (quest log) shows names/goals only; agendas and
  detail are keeper-only
"""
