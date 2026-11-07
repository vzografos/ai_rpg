"""Story spine (M3): the hidden scaffold that keeps the story honest.

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
- generated at session start (or from the pre-authored adventure JSON),
  schema-validated before use (`from_dict`)
- clamped refresh at beat completion: may mark done/skipped, append
  annotations, add sub-threads, tick waypoints; may never rewrite a
  completed beat (`apply_delta` enforces it — replay-safe)
- waypoints are ticked by the SERVER from the DM's own tool calls (world
  state + recorded facts — `progress_events`); the story advances through
  real play, not model whim
- the spine lives in the event log (`spine_set` at session start, then
  `spine_delta` ops) so kill+restart reproduces it exactly (M2 authority)
- player-safe projection (quest log) shows beat text only; cast agendas,
  event details and beat details are keeper-only (`now_block` is DM-only)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # runtime duck-typed; importing here would cycle engine -> dm
    from rpg.engine.state import WorldState

__all__ = [
    "BEAT_STATUSES",
    "WAYPOINT_KINDS",
    "SPINE_EVENT_STATUSES",
    "Waypoint",
    "Beat",
    "CastMember",
    "SpineEvent",
    "StorySpine",
    "Adventure",
    "load_adventure",
    "adventure_init_payload",
]

BEAT_STATUSES = ("pending", "active", "done", "skipped")
WAYPOINT_KINDS = ("place", "npc", "item", "objective", "fight", "narrative")
SPINE_EVENT_STATUSES = ("pending", "triggered")

FINAL_STATUSES = ("done", "skipped")


def _req_str(raw: dict[str, Any], key: str, *, default: str = "") -> str:
    value = raw.get(key, default)
    if not isinstance(value, str):
        raise ValueError(f"spine field {key!r} must be a string, got {type(value).__name__}")
    return value


def _req_int(raw: dict[str, Any], key: str, *, default: int = 1) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"spine field {key!r} must be an int, got {value!r}")
    return value


def _req_bool(raw: dict[str, Any], key: str, *, default: bool = False) -> bool:
    value = raw.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"spine field {key!r} must be a bool, got {value!r}")
    return value


def _npc_display_name(reference: str) -> str:
    """Map a recorded ``npc:<name>`` id to the cast's display name."""
    return reference.removeprefix("npc:").strip()


def _req_list(raw: dict[str, Any], key: str, *, default: list | None = None) -> list:
    value = raw.get(key, default)
    if not isinstance(value, list):
        raise ValueError(f"spine field {key!r} must be a list, got {type(value).__name__}")
    return value


def _req_nonempty_str(raw: dict[str, Any], key: str) -> str:
    value = _req_str(raw, key)
    if not value:
        raise ValueError(f"spine field {key!r} must be non-empty")
    return value


# --- pieces --------------------------------------------------------------------


@dataclass
class Waypoint:
    """A gate on the active beat: ticked by the server, never by the model.

    `kind` picks the tick rule (see `StorySpine.progress_events`):
    place -> the PC is there; item -> the PC holds it;
    npc/objective/fight/narrative -> a fact with that value was recorded.
    """

    kind: str
    text: str
    done: bool = False

    def __post_init__(self) -> None:
        if self.kind not in WAYPOINT_KINDS:
            raise ValueError(f"waypoint kind must be one of {WAYPOINT_KINDS!r}: {self.kind!r}")
        if not self.text:
            raise ValueError("waypoint text must be non-empty")

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "text": self.text, "done": self.done}


@dataclass
class Beat:
    """One story beat. `detail` accretes via append-only annotations."""

    text: str
    status: str = "pending"
    act: int = 1
    waypoints: list[Waypoint] = field(default_factory=list)
    detail: str = ""

    def __post_init__(self) -> None:
        if not self.text:
            raise ValueError("beat text must be non-empty")
        if self.status not in BEAT_STATUSES:
            raise ValueError(f"beat status must be one of {BEAT_STATUSES!r}: {self.status!r}")
        if self.act < 1:
            raise ValueError(f"beat act must be >= 1: {self.act}")
        for waypoint in self.waypoints:
            Waypoint(waypoint.kind, waypoint.text, waypoint.done)

    @property
    def is_final(self) -> bool:
        return self.status in FINAL_STATUSES

    @property
    def all_waypoints_done(self) -> bool:
        return all(waypoint.done for waypoint in self.waypoints)

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "status": self.status,
            "act": self.act,
            "waypoints": [waypoint.to_dict() for waypoint in self.waypoints],
            "detail": self.detail,
        }

    def player_dict(self) -> dict[str, Any]:
        """The player-safe projection: no keeper-only `detail`."""
        return {
            "text": self.text,
            "status": self.status,
            "act": self.act,
            "waypoints": [waypoint.to_dict() for waypoint in self.waypoints],
        }


@dataclass
class CastMember:
    """A story-cast member. `agenda` is keeper-only: it never reaches the
    player-safe projection, only the DM's `[NOW]` block."""

    name: str
    role: str = ""
    agenda: str = ""
    status: str = "present"

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("cast name must be non-empty")
        if not self.status:
            raise ValueError("cast status must be non-empty")

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "role": self.role, "agenda": self.agenda, "status": self.status}


@dataclass
class SpineEvent:
    """A planned story event. `detail` is keeper-only; `trigger` is a soft
    fiction-level description of when it fires."""

    kind: str
    name: str
    detail: str = ""
    trigger: str = ""
    status: str = "pending"

    def __post_init__(self) -> None:
        if not self.kind:
            raise ValueError("spine event kind must be non-empty")
        if not self.name:
            raise ValueError("spine event name must be non-empty")
        if self.status not in SPINE_EVENT_STATUSES:
            raise ValueError(
                f"spine event status must be one of {SPINE_EVENT_STATUSES!r}: {self.status!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "name": self.name,
            "detail": self.detail,
            "trigger": self.trigger,
            "status": self.status,
        }


# --- the spine -------------------------------------------------------------------


class StorySpine:
    """The hidden story scaffold. Mutations only through `apply_delta`
    (clamped ops — the same ones the event log replays) so the spine can
    never drift from its log."""

    def __init__(
        self,
        *,
        premise: str,
        stakes: str,
        antagonist: str,
        beats: list[Beat],
        cast: list[CastMember] = (),
        events: list[SpineEvent] = (),
        finale: str = "",
    ) -> None:
        if not premise:
            raise ValueError("spine premise must be non-empty")
        if not stakes:
            raise ValueError("spine stakes must be non-empty")
        if not antagonist:
            raise ValueError("spine antagonist must be non-empty")
        if not finale:
            raise ValueError("spine finale must be non-empty")
        if not beats:
            raise ValueError("a spine needs at least one beat")
        self.premise = premise
        self.stakes = stakes
        self.antagonist = antagonist
        self.beats = list(beats)
        self.cast = list(cast)
        self.events = list(events)
        self.finale = finale
        self._ensure_active()

    # -- construction ---------------------------------------------------------

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> StorySpine:
        """Schema-validate a spine dict (generated call or adventure JSON)."""
        if not isinstance(raw, dict):
            raise ValueError(f"spine must be an object, got {type(raw).__name__}")
        beats = []
        for beat_raw in _req_list(raw, "beats"):
            if not isinstance(beat_raw, dict):
                raise ValueError(f"beat must be an object: {beat_raw!r}")
            waypoints = []
            for wp_raw in _req_list(beat_raw, "waypoints", default=[]):
                if not isinstance(wp_raw, dict):
                    raise ValueError(f"waypoint must be an object: {wp_raw!r}")
                waypoints.append(
                    Waypoint(
                        kind=_req_str(wp_raw, "kind"),
                        text=_req_nonempty_str(wp_raw, "text"),
                        done=_req_bool(wp_raw, "done"),
                    )
                )
            beats.append(
                Beat(
                    text=_req_nonempty_str(beat_raw, "text"),
                    status=_req_str(beat_raw, "status", default="pending"),
                    act=_req_int(beat_raw, "act"),
                    waypoints=waypoints,
                    detail=_req_str(beat_raw, "detail"),
                )
            )
        cast = []
        for member_raw in _req_list(raw, "cast", default=[]):
            if not isinstance(member_raw, dict):
                raise ValueError(f"cast member must be an object: {member_raw!r}")
            cast.append(
                CastMember(
                    name=_req_nonempty_str(member_raw, "name"),
                    role=_req_str(member_raw, "role"),
                    agenda=_req_str(member_raw, "agenda"),
                    status=_req_str(member_raw, "status", default="present"),
                )
            )
        events = []
        for event_raw in _req_list(raw, "events", default=[]):
            if not isinstance(event_raw, dict):
                raise ValueError(f"spine event must be an object: {event_raw!r}")
            events.append(
                SpineEvent(
                    kind=_req_nonempty_str(event_raw, "kind"),
                    name=_req_nonempty_str(event_raw, "name"),
                    detail=_req_str(event_raw, "detail"),
                    trigger=_req_str(event_raw, "trigger"),
                    status=_req_str(event_raw, "status", default="pending"),
                )
            )
        return cls(
            premise=_req_nonempty_str(raw, "premise"),
            stakes=_req_nonempty_str(raw, "stakes"),
            antagonist=_req_nonempty_str(raw, "antagonist"),
            beats=beats,
            cast=cast,
            events=events,
            finale=_req_nonempty_str(raw, "finale"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "premise": self.premise,
            "stakes": self.stakes,
            "antagonist": self.antagonist,
            "beats": [beat.to_dict() for beat in self.beats],
            "cast": [member.to_dict() for member in self.cast],
            "events": [event.to_dict() for event in self.events],
            "finale": self.finale,
        }

    # -- queries ---------------------------------------------------------------

    def beat(self, index: int) -> Beat:
        try:
            return self.beats[index]
        except IndexError:
            raise ValueError(f"no beat at index {index}") from None

    @property
    def active_beat(self) -> Beat | None:
        for beat in self.beats:
            if beat.status == "active":
                return beat
        return None

    @property
    def is_over(self) -> bool:
        return all(beat.is_final for beat in self.beats)

    def _ensure_active(self) -> None:
        active = [beat for beat in self.beats if beat.status == "active"]
        if len(active) > 1:
            raise ValueError("a spine has at most one active beat")
        if not active:
            for beat in self.beats:
                if beat.status == "pending":
                    beat.status = "active"
                    return

    # -- clamped mutations (the only writer; replay-safe) ----------------------

    def apply_delta(self, op: str, payload: dict[str, Any]) -> None:
        """Apply one clamped spine op (the `spine_delta` event vocabulary).

        Every op is validated against the current spine state; a violation is
        a ValueError — the event log rejects it on replay exactly as the
        live loop did.
        """
        handler = getattr(self, f"_delta_{op}", None)
        if handler is None:
            raise ValueError(f"unknown spine op: {op!r}")
        handler(payload)

    def _delta_beat_status(self, p: dict[str, Any]) -> None:
        index = _req_int(p, "beat")
        status = _req_str(p, "status")
        if status not in FINAL_STATUSES:
            raise ValueError(f"beat_status only marks a beat final: {status!r}")
        beat = self.beat(index)
        if beat.is_final:
            raise ValueError(f"beat {index} is already {beat.status!r} — completed beats are final")
        beat.status = status
        self._activate_next()

    def _activate_next(self) -> None:
        for beat in self.beats:
            if beat.status == "pending":
                beat.status = "active"
                return

    def _delta_append_detail(self, p: dict[str, Any]) -> None:
        index = _req_int(p, "beat")
        text = _req_nonempty_str(p, "text")
        beat = self.beat(index)
        if not beat.detail:
            beat.detail = text
        else:
            beat.detail += f"\n{text}"

    def _delta_tick_waypoint(self, p: dict[str, Any]) -> None:
        index = _req_int(p, "beat")
        waypoint_index = _req_int(p, "waypoint")
        beat = self.beat(index)
        try:
            waypoint = beat.waypoints[waypoint_index]
        except IndexError:
            raise ValueError(f"beat {index} has no waypoint {waypoint_index}") from None
        if waypoint.done:
            raise ValueError(f"waypoint already done: {waypoint.text!r}")
        waypoint.done = True

    def _delta_rewrite_beat(self, p: dict[str, Any]) -> None:
        index = _req_int(p, "beat")
        text = _req_nonempty_str(p, "text")
        beat = self.beat(index)
        if beat.is_final:
            raise ValueError(f"cannot rewrite a completed beat: {index}")
        active = self.active_beat
        if active is None:
            active = self.beats[0]
        if beat.act != active.act:
            raise ValueError(f"cannot rewrite a beat from another act: {index}")

        beat.text = text

    # -- server-ticked progress ---------------------------------------------------

    def progress_events(self, world: WorldState) -> list[dict[str, Any]]:
        """Clamped deltas the world state justifies (the server's tick).

        Only the active beat's waypoints are considered. Tick rules (the
        waypoint `kind` selects the evidence):
        - place: the PC is at the named location
        - item:  the PC holds the named item
        - npc / objective / fight / narrative: a fact with that value exists
          (recorded through the `note` tool — real play, not model whim)

        Returns the `spine_delta` payloads to commit, in order; applying
        them (then `advance_events`) is what moves the story forward.
        """
        beat = self.active_beat
        if beat is None or world.pc is None:
            return []
        facts = {fact.value for fact in world.facts}
        # Models sometimes use the world's namespaced NPC id (``npc:Brenna``)
        # as the spoke_with value/subject instead of the display name. Treat
        # that recorded identifier as the same NPC waypoint, but only when the
        # fact explicitly records a conversation.
        spoken_to = {
            _npc_display_name(reference)
            for fact in world.facts
            if fact.predicate == "spoke_with"
            for reference in (fact.subject, fact.value)
            if reference.startswith("npc:")
        }
        deltas: list[dict[str, Any]] = []
        for waypoint_index, waypoint in enumerate(beat.waypoints):
            if waypoint.done:
                continue
            if self._waypoint_met(waypoint, world, facts, spoken_to):
                deltas.append(
                    {
                        "op": "tick_waypoint",
                        "beat": self.beats.index(beat),
                        "waypoint": waypoint_index,
                    }
                )
        return deltas

    @staticmethod
    def _waypoint_met(
        waypoint: Waypoint,
        world: WorldState,
        facts: set[str],
        spoken_to: set[str],
    ) -> bool:
        if waypoint.kind == "place":
            return world.pc is not None and world.pc.location == waypoint.text
        if waypoint.kind == "item":
            item = world.items.get(waypoint.text)
            if item is None or world.pc is None:
                return False
            return item.holder == f"pc:{world.pc.sheet.name}"
        if waypoint.kind == "npc":
            return waypoint.text in facts or waypoint.text in spoken_to
        return waypoint.text in facts

    def advance_events(self) -> list[dict[str, Any]]:
        """Clamped deltas for a completed active beat (chapter close).

        When every active-beat waypoint is done, the beat is marked done and
        the next pending beat activates — deterministically, on replay too.
        """
        beat = self.active_beat
        if beat is None or not beat.all_waypoints_done:
            return []
        return [{"op": "beat_status", "beat": self.beats.index(beat), "status": "done"}]

    # -- projections ------------------------------------------------------------

    def now_block(self) -> str:
        """The DM-only `[NOW]` block (stable head; never player-facing).

        Active beat + waypoint checklist + next up + cast agendas + pending
        event triggers + finale. Keeper discipline (the prompt) keeps this
        out of the narration; the player sees only the quest projection.
        """
        lines = [
            f"Premise: {self.premise}",
            f"Stakes: {self.stakes}",
            f"Antagonist: {self.antagonist}",
        ]
        beat = self.active_beat
        if beat is None:
            lines.append("The story is over — the finale has been played.")
        else:
            index = self.beats.index(beat)
            lines.append(
                f"NOW (beat {index + 1} of {len(self.beats)}, act {beat.act}): {beat.text}"
            )
            for waypoint in beat.waypoints:
                mark = "x" if waypoint.done else " "
                lines.append(f"  [{mark}] {waypoint.kind}: {waypoint.text}")
            if beat.detail:
                lines.append(f"  Notes: {beat.detail}")
            upcoming = [b for b in self.beats[index + 1 :] if not b.is_final]
            if upcoming:
                lines.append(f"Up next: {upcoming[0].text}")
        if self.cast:
            lines.append("Cast (agendas are yours to work, never to reveal):")
            for member in self.cast:
                suffix = f" — {member.agenda}" if member.agenda else ""
                state = f" ({member.status})" if member.status != "present" else ""
                lines.append(f"  {member.name} ({member.role}){state}{suffix}")
        pending = [event for event in self.events if event.status == "pending"]
        if pending:
            lines.append("Planned events (soft triggers — fiction terms):")
            for event in pending:
                lines.append(f"  {event.name} — when: {event.trigger}")
        lines.append(f"Finale: {self.finale}")
        return "\n".join(lines)

    def quest_lines(self) -> list[str]:
        """Player-safe projection (the sidebar quest log): beat text only.

        No agendas, no event details, no beat details — names and goals only.
        """
        lines: list[str] = []
        active = self.active_beat
        if active is not None:
            lines.append(f"Current: {active.text}")
            for waypoint in active.waypoints:
                if not waypoint.done:
                    lines.append(f"  - {waypoint.text}")
        upcoming = [b for b in self.beats if b.status == "pending"]
        if upcoming:
            lines.append(f"Next: {upcoming[0].text}")
        return lines


# --- the adventure bundle -----------------------------------------------------------


@dataclass
class Adventure:
    """A pre-authored adventure: spine + world seed + encounter stat blocks.

    The world seed (pc template, locations, npcs, items) feeds `world_init`;
    the encounters are DM-facing stat blocks for `start_combat` (their
    `notes` are keeper-only, like agendas).
    """

    title: str
    spine: StorySpine
    pc: dict[str, Any]
    pc_location: str
    locations: list[dict[str, Any]]
    npcs: list[dict[str, Any]]
    items: list[dict[str, Any]]
    encounters: list[dict[str, Any]]

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Adventure:
        if not isinstance(raw, dict):
            raise ValueError(f"adventure must be an object, got {type(raw).__name__}")
        title = _req_nonempty_str(raw, "title")
        spine_raw = raw.get("spine")
        if not isinstance(spine_raw, dict):
            raise ValueError("adventure requires a spine object")
        pc = raw.get("pc")
        if not isinstance(pc, dict) or not pc.get("name"):
            raise ValueError("adventure requires a pc template with a name")
        locations = _req_list(raw, "locations")
        npcs = _req_list(raw, "npcs")
        items = _req_list(raw, "items", default=[])
        encounters = _req_list(raw, "encounters")
        if not 3 <= len(locations) <= 5:
            raise ValueError(f"adventure needs 3-5 locations, got {len(locations)}")
        if not 6 <= len(npcs) <= 10:
            raise ValueError(f"adventure needs 6-10 npcs, got {len(npcs)}")
        if not encounters:
            raise ValueError("adventure needs at least one encounter")
        for npc in npcs:
            if not isinstance(npc, dict) or not _req_str(npc, "agenda"):
                raise ValueError(f"every npc needs a keeper-only agenda: {npc!r}")
        location_names = set()
        for location in locations:
            if not isinstance(location, dict):
                raise ValueError(f"location must be an object: {location!r}")
            name = _req_nonempty_str(location, "name")
            if name in location_names:
                raise ValueError(f"duplicate location: {name!r}")
            location_names.add(name)
        npc_names = set()
        for npc in npcs:
            name = _req_nonempty_str(npc, "name")
            if name in npc_names:
                raise ValueError(f"duplicate npc: {name!r}")
            npc_names.add(name)
        for encounter in encounters:
            if not isinstance(encounter, dict):
                raise ValueError(f"encounter must be an object: {encounter!r}")
            _req_nonempty_str(encounter, "name")
            participants = _req_list(encounter, "participants")
            if not participants:
                raise ValueError(f"encounter {encounter.get('name')!r} needs participants")
            for participant in participants:
                if not isinstance(participant, dict):
                    raise ValueError(f"combatant must be an object: {participant!r}")
                _req_nonempty_str(participant, "name")
                _req_int(participant, "ac")
                _req_int(participant, "hp")
                _req_int(participant, "max_hp")
                _req_int(participant, "dexterity")
                _req_int(participant, "attack_bonus")
                _req_nonempty_str(participant, "damage")
        return cls(
            title=title,
            spine=StorySpine.from_dict(spine_raw),
            pc=pc,
            pc_location=_req_nonempty_str(raw, "pc_location"),
            locations=locations,
            npcs=npcs,
            items=items,
            encounters=encounters,
        )


def load_adventure(path: str | Path) -> Adventure:
    """Load + validate the pre-authored adventure JSON (the M3 content)."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return Adventure.from_dict(raw)


def adventure_init_payload(adventure: Adventure, *, pc_name: str | None = None) -> dict[str, Any]:
    """The `world_init` event payload seeded from an adventure.

    The pc template's numbers are the pre-authored character; only the name
    may be overridden (the player's own character, same sheet).
    """
    pc = dict(adventure.pc)
    if pc_name is not None:
        if not pc_name:
            raise ValueError("pc name must be non-empty")
        pc["name"] = pc_name
    # The description travels to the PlayerCharacter, not the sheet.
    pc_description = pc.pop("description", "")
    return {
        "pc": pc,
        "pc_location": adventure.pc_location,
        "pc_description": pc_description,
        "locations": adventure.locations,
        "npcs": adventure.npcs,
        "quests": [],
        "items": adventure.items,
    }
