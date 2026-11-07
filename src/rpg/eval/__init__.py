"""M6 play-quality eval (eval layer 3).

`archetypes` holds the scripted players; `judge` scores each committed turn
(structurally, and optionally with a second model); `run` plays one session
per archetype and writes the report (JSON + Markdown) — the M6 learning
deliverable.
"""

from rpg.eval.archetypes import ARCHETYPES, Archetype
from rpg.eval.judge import ArchetypeReport, TurnVerdict
from rpg.eval.run import run_archetype, write_report

__all__ = [
    "ARCHETYPES",
    "Archetype",
    "ArchetypeReport",
    "TurnVerdict",
    "run_archetype",
    "write_report",
]
