"""World state (roadmap M2): entities in the SQLite-backed world.

Entities (the state/memory layout): pc, npc, item, quest, location, fact — plus the
current combat kept as *committed* records (results, not re-rolls).

`WorldState` is the live, queryable view. Every mutation enters through
`apply_event(kind, payload, turn)` — the single validated, clamped writer
(hard constraint: the LLM never owns numbers). The append-only event log
(events.py) is the authority, and `rebuild_world(events)` must reproduce
this state exactly (property test, M2 acceptance).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from rpg.dm.spine import StorySpine
from rpg.engine.combat import CONDITIONS
from rpg.engine.dice import parse_formula
from rpg.engine.sheet import CharacterSheet, ability_modifier

__all__ = [
    "NPC_STATUSES",
    "QUEST_STATUSES",
    "COMBAT_OUTCOMES",
    "PlayerCharacter",
    "Npc",
    "Item",
    "Quest",
    "Location",
    "Fact",
    "CombatantRecord",
    "CombatState",
    "WorldState",
]

NPC_STATUSES = ("alive", "dead")
QUEST_STATUSES = ("open", "active", "done", "skipped")
COMBAT_OUTCOMES = ("victory", "defeat", "fled")


# --- payload helpers (events carry JSON; the engine validates every field) --


def _req_str(payload: dict[str, Any], key: str, *, default: str = "") -> str:
    value = payload.get(key, default)
    if not isinstance(value, str):
        raise ValueError(f"event field {key!r} must be a string, got {type(value).__name__}")
    return value


def _req_int(payload: dict[str, Any], key: str, *, default: int | None = None) -> int:
    value = payload.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"event field {key!r} must be an int, got {value!r}")
    return value


def _req_bool(payload: dict[str, Any], key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"event field {key!r} must be a bool, got {value!r}")
    return value


def _req_list(payload: dict[str, Any], key: str, *, default: list | None = None) -> list:
    value = payload.get(key, default)
    if not isinstance(value, list):
        raise ValueError(f"event field {key!r} must be a list, got {type(value).__name__}")
    return value


def _note(turn: int, text: str) -> dict[str, Any]:
    return {"turn": turn, "text": text}


def _validate_notes(notes: list[dict[str, Any]]) -> None:
    for entry in notes:
        if not isinstance(entry, dict) or not isinstance(entry.get("turn"), int):
            raise ValueError(f"note entry must be {{turn: int, text: str}}: {entry!r}")
        if not isinstance(entry.get("text"), str) or not entry["text"]:
            raise ValueError(f"note entry text must be a non-empty string: {entry!r}")


# --- entities -----------------------------------------------------------------


@dataclass
class PlayerCharacter:
    """The single PC: the M1 sheet plus where it stands and keeper notes."""

    sheet: CharacterSheet
    location: str = ""
    description: str = ""
    notes: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        _validate_notes(self.notes)


@dataclass
class Npc:
    """An NPC. `agenda` is keeper-only: it never enters the volatile tail."""

    name: str
    location: str = ""
    description: str = ""
    status: str = "alive"
    agenda: str = ""
    notes: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("npc name must be non-empty")
        if self.status not in NPC_STATUSES:
            raise ValueError(f"npc status must be one of {NPC_STATUSES!r}: {self.status!r}")
        _validate_notes(self.notes)


@dataclass
class Item:
    """An item. `holder` is "", "pc:<name>", or "npc:<name>".

    An item is either held (holder set) or at a place (holder empty); the
    two never mix.
    """

    name: str
    holder: str = ""
    location: str = ""
    quantity: int = 1
    description: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("item name must be non-empty")
        if self.quantity < 1:
            raise ValueError(f"item quantity must be >= 1: {self.quantity}")
        if self.holder and self.location:
            raise ValueError(f"item {self.name!r} is held and placed at once")
        if self.holder and not self.holder.startswith(("pc:", "npc:")):
            raise ValueError(f"item holder must be 'pc:<name>' or 'npc:<name>': {self.holder!r}")


@dataclass
class Quest:
    """A tracked objective. Status moves one way only (done/skipped are final)."""

    name: str
    description: str = ""
    status: str = "open"
    reward: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("quest name must be non-empty")
        if self.status not in QUEST_STATUSES:
            raise ValueError(f"quest status must be one of {QUEST_STATUSES!r}: {self.status!r}")


@dataclass
class Location:
    name: str
    description: str = ""
    exits: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("location name must be non-empty")


@dataclass(frozen=True)
class Fact:
    """A recalled fact: (subject, predicate, value) + when + why (M5: FTS5)."""

    subject: str
    predicate: str
    value: str
    turn: int
    source: str = ""

    def __post_init__(self) -> None:
        fields = (("subject", self.subject), ("predicate", self.predicate), ("value", self.value))
        for label, value in fields:
            if not value:
                raise ValueError(f"fact {label} must be non-empty: {value!r}")
        if self.turn < 0:
            raise ValueError(f"fact turn must be >= 0: {self.turn}")


@dataclass
class CombatantRecord:
    """A committed combatant (M1 `Combatant` data, minus the RNG).

    The durable record of an in-combat creature: every number here was
    rolled or clamped by the engine and committed to the event log.
    """

    name: str
    ac: int
    hp: int
    max_hp: int
    dexterity: int
    attack_bonus: int
    damage: str
    conditions: frozenset[str] = frozenset()
    initiative: int = 0
    death_successes: int = 0
    death_failures: int = 0

    def __post_init__(self) -> None:
        self.conditions = frozenset(self.conditions)
        if not self.name:
            raise ValueError("combatant name must be non-empty")
        if self.max_hp < 1:
            raise ValueError(f"max_hp must be >= 1: {self.max_hp}")
        if not 0 <= self.hp <= self.max_hp:
            raise ValueError(f"hp must be 0..max_hp: {self.hp}")
        if self.ac < 1:
            raise ValueError(f"ac must be >= 1: {self.ac}")
        ability_modifier(self.dexterity)
        parse_formula(self.damage)
        unknown = self.conditions - set(CONDITIONS)
        if unknown:
            raise ValueError(f"unknown condition(s): {sorted(unknown)!r}")
        counters = (
            ("death_successes", self.death_successes),
            ("death_failures", self.death_failures),
        )
        for label, value in counters:
            if not 0 <= value <= 3:
                raise ValueError(f"{label} must be 0..3: {value}")

    @property
    def is_dead(self) -> bool:
        return self.hp == 0 and self.death_failures >= 3


@dataclass
class CombatState:
    """The current encounter as committed records (turn order, HP, conditions)."""

    participants: dict[str, CombatantRecord]
    order: list[str]
    index: int
    round: int
    outcome: str | None = None

    def __post_init__(self) -> None:
        if len(self.order) < 2:
            raise ValueError("an encounter needs at least 2 combatants")
        if sorted(self.order) != sorted(self.participants):
            raise ValueError("combat order must name exactly the participants")
        if len(set(self.order)) != len(self.order):
            raise ValueError("combat order must be unique")
        if not 0 <= self.index < len(self.order):
            raise ValueError(f"combat index out of range: {self.index}")
        if self.round < 0:
            raise ValueError(f"combat round must be >= 0: {self.round}")
        if self.outcome is not None and self.outcome not in COMBAT_OUTCOMES:
            raise ValueError(f"combat outcome must be one of {COMBAT_OUTCOMES!r}: {self.outcome!r}")

    @property
    def current(self) -> str:
        return self.order[self.index]


# --- the world ----------------------------------------------------------------


class WorldState:
    """The live world: entities + current combat + turn counter + summary.

    `apply_event` is the only writer. Kinds and payloads are the event
    vocabulary of events.py (EVENT_KINDS); every field is validated and
    every number clamped before it touches state.
    """

    def __init__(self) -> None:
        self.pc: PlayerCharacter | None = None
        self.npcs: dict[str, Npc] = {}
        self.items: dict[str, Item] = {}
        self.quests: dict[str, Quest] = {}
        self.locations: dict[str, Location] = {}
        self.facts: list[Fact] = []
        self.combat: CombatState | None = None
        self.spine: StorySpine | None = None
        self.turn: int = 0
        self.summary: str = ""
        self.summary_watermark: int = 0

    # -- queries ---------------------------------------------------------------

    @property
    def is_empty(self) -> bool:
        return (
            self.pc is None
            and not self.npcs
            and not self.items
            and not self.quests
            and not self.locations
            and not self.facts
            and self.combat is None
            and self.spine is None
        )

    def npc(self, name: str) -> Npc:
        try:
            return self.npcs[name]
        except KeyError:
            raise ValueError(f"unknown npc: {name!r}") from None

    def quest(self, name: str) -> Quest:
        try:
            return self.quests[name]
        except KeyError:
            raise ValueError(f"unknown quest: {name!r}") from None

    def location(self, name: str) -> Location:
        try:
            return self.locations[name]
        except KeyError:
            raise ValueError(f"unknown location: {name!r}") from None

    def item(self, name: str) -> Item:
        try:
            return self.items[name]
        except KeyError:
            raise ValueError(f"unknown item: {name!r}") from None

    def combatant(self, name: str) -> CombatantRecord:
        if self.combat is None:
            raise ValueError("no combat in progress")
        try:
            return self.combat.participants[name]
        except KeyError:
            raise ValueError(f"not in this combat: {name!r}") from None

    # -- the single writer ------------------------------------------------------

    def apply_event(self, kind: str, payload: dict[str, Any], turn: int) -> None:
        """Apply one committed event (validated, clamped). The only mutator."""
        handler = getattr(self, f"_apply_{kind}", None)
        if handler is None:
            raise ValueError(f"unknown event kind: {kind!r}")
        handler(payload, turn)

    # -- world setup ------------------------------------------------------------

    def _apply_world_init(self, p: dict[str, Any], turn: int) -> None:
        if not self.is_empty:
            raise ValueError("world_init on a non-empty world")
        pc_payload = p.get("pc")
        if not isinstance(pc_payload, dict) or not pc_payload.get("name"):
            raise ValueError("world_init requires a pc with a name")
        # Locations first: the pc, npcs, items may reference them. Exits may
        # name sibling locations from the same init (validated as a set).
        raw_locations = _req_list(p, "locations", default=[])
        pending = {raw.get("name") for raw in raw_locations if isinstance(raw, dict)}
        for raw in raw_locations:
            self._apply_location_add(raw, turn, pending=pending)
        for raw in _req_list(p, "npcs", default=[]):
            self._apply_npc_add(raw, turn)
        for raw in _req_list(p, "quests", default=[]):
            self._apply_quest_add(raw, turn)
        for raw in _req_list(p, "items", default=[]):
            self._apply_item_add(raw, turn)
        sheet = CharacterSheet(**pc_payload)
        self.pc = PlayerCharacter(
            sheet=sheet,
            location=_req_str(p, "pc_location", default=""),
            description=_req_str(p, "pc_description"),
        )
        if self.pc.location:
            self._require_location(self.pc.location)

    # -- pc ---------------------------------------------------------------------

    def _require_pc(self) -> PlayerCharacter:
        if self.pc is None:
            raise ValueError("no player character in this world")
        return self.pc

    def _apply_pc_damage(self, p: dict[str, Any], turn: int) -> None:
        amount = _req_int(p, "amount")
        self._require_pc().sheet.take_damage(amount)

    def _apply_pc_heal(self, p: dict[str, Any], turn: int) -> None:
        amount = _req_int(p, "amount")
        self._require_pc().sheet.heal(amount)

    def _apply_pc_set_hp(self, p: dict[str, Any], turn: int) -> None:
        hp = _req_int(p, "hp")
        sheet = self._require_pc().sheet
        if not 0 <= hp <= sheet.max_hp:
            raise ValueError(f"pc hp must be 0..max_hp: {hp}")
        sheet.hp = hp

    def _apply_pc_gain_xp(self, p: dict[str, Any], turn: int) -> None:
        amount = _req_int(p, "amount")
        self._require_pc().sheet.add_xp(amount)

    def _apply_pc_gold(self, p: dict[str, Any], turn: int) -> None:
        amount = _req_int(p, "amount")
        self._require_pc().sheet.add_gold(amount)

    def _apply_pc_item_add(self, p: dict[str, Any], turn: int) -> None:
        sheet = self._require_pc().sheet
        sheet.add_item(_req_str(p, "name"), _req_int(p, "quantity", default=1))

    def _apply_pc_item_remove(self, p: dict[str, Any], turn: int) -> None:
        sheet = self._require_pc().sheet
        sheet.remove_item(_req_str(p, "name"), _req_int(p, "quantity", default=1))

    def _apply_pc_move(self, p: dict[str, Any], turn: int) -> None:
        pc = self._require_pc()
        location = _req_str(p, "location")
        self._require_location(location)
        pc.location = location

    def _apply_pc_note(self, p: dict[str, Any], turn: int) -> None:
        text = _req_str(p, "text")
        if not text:
            raise ValueError("pc_note text must be non-empty")
        self._require_pc().notes.append(_note(turn, text))

    def _require_location(self, name: str) -> Location:
        try:
            return self.locations[name]
        except KeyError:
            raise ValueError(f"unknown location: {name!r}") from None

    # -- npcs ---------------------------------------------------------------------

    def _apply_npc_add(self, p: dict[str, Any], turn: int) -> None:
        name = _req_str(p, "name")
        if not name:
            raise ValueError("npc_add name must be non-empty")
        if name in self.npcs:
            raise ValueError(f"npc already exists: {name!r}")
        npc = Npc(
            name=name,
            location=_req_str(p, "location"),
            description=_req_str(p, "description"),
            status=_req_str(p, "status", default="alive"),
            agenda=_req_str(p, "agenda"),
        )
        if npc.location:
            self._require_location(npc.location)
        self.npcs[name] = npc

    def _apply_npc_note(self, p: dict[str, Any], turn: int) -> None:
        text = _req_str(p, "text")
        if not text:
            raise ValueError("npc_note text must be non-empty")
        self.npc(_req_str(p, "name")).notes.append(_note(turn, text))

    def _apply_npc_set_status(self, p: dict[str, Any], turn: int) -> None:
        npc = self.npc(_req_str(p, "name"))
        status = _req_str(p, "status")
        if status not in NPC_STATUSES:
            raise ValueError(f"npc status must be one of {NPC_STATUSES!r}: {status!r}")
        if npc.status == "dead" and status == "alive":
            raise ValueError(f"the dead stay dead: {npc.name!r}")
        npc.status = status

    def _apply_npc_set_location(self, p: dict[str, Any], turn: int) -> None:
        npc = self.npc(_req_str(p, "name"))
        npc.location = _req_str(p, "location")
        if npc.location:
            self._require_location(npc.location)

    # -- items ----------------------------------------------------------------------

    def _apply_item_add(self, p: dict[str, Any], turn: int) -> None:
        name = _req_str(p, "name")
        if not name:
            raise ValueError("item_add name must be non-empty")
        if name in self.items:
            raise ValueError(f"item already exists: {name!r}")
        item = Item(
            name=name,
            holder=_req_str(p, "holder"),
            location=_req_str(p, "location"),
            quantity=_req_int(p, "quantity", default=1),
            description=_req_str(p, "description"),
        )
        self._require_holder(item.holder, item.name)
        if item.location:
            self._require_location(item.location)
        self.items[name] = item

    def _apply_item_set_holder(self, p: dict[str, Any], turn: int) -> None:
        item = self.item(_req_str(p, "name"))
        item.holder = _req_str(p, "holder")
        item.location = ""  # an item is held XOR placed; re-place it with a later event
        self._require_holder(item.holder, item.name)

    def _require_holder(self, holder: str, item_name: str) -> None:
        if not holder:
            return
        if holder.startswith("pc:"):
            if self.pc is None or holder[3:] != self.pc.sheet.name:
                raise ValueError(f"item {item_name!r}: unknown holder {holder!r}")
        elif holder.startswith("npc:"):
            if holder[4:] not in self.npcs:
                raise ValueError(f"item {item_name!r}: unknown holder {holder!r}")

    # -- quests ---------------------------------------------------------------------

    def _apply_quest_add(self, p: dict[str, Any], turn: int) -> None:
        name = _req_str(p, "name")
        if not name:
            raise ValueError("quest_add name must be non-empty")
        if name in self.quests:
            raise ValueError(f"quest already exists: {name!r}")
        self.quests[name] = Quest(
            name=name,
            description=_req_str(p, "description"),
            status=_req_str(p, "status", default="open"),
            reward=_req_str(p, "reward"),
        )

    def _apply_quest_set_status(self, p: dict[str, Any], turn: int) -> None:
        quest = self.quest(_req_str(p, "name"))
        status = _req_str(p, "status")
        if status not in QUEST_STATUSES:
            raise ValueError(f"quest status must be one of {QUEST_STATUSES!r}: {status!r}")
        if quest.status in ("done", "skipped"):
            raise ValueError(f"quest {quest.name!r} is already {quest.status} (final)")
        quest.status = status

    # -- locations -------------------------------------------------------------------

    def _apply_location_add(
        self, p: dict[str, Any], turn: int, *, pending: set[str] | None = None
    ) -> None:
        name = _req_str(p, "name")
        if not name:
            raise ValueError("location_add name must be non-empty")
        if name in self.locations:
            raise ValueError(f"location already exists: {name!r}")
        exits = _req_list(p, "exits", default=[])
        known = set(self.locations) | (pending or set())
        for exit_name in exits:
            if not isinstance(exit_name, str) or exit_name not in known:
                raise ValueError(f"unknown exit: {exit_name!r}")
        description = _req_str(p, "description")
        self.locations[name] = Location(name=name, description=description, exits=list(exits))

    # -- facts ------------------------------------------------------------------------

    def _apply_fact_add(self, p: dict[str, Any], turn: int) -> None:
        self.facts.append(
            Fact(
                subject=_req_str(p, "subject"),
                predicate=_req_str(p, "predicate"),
                value=_req_str(p, "value"),
                turn=turn,
                source=_req_str(p, "source"),
            )
        )

    def _apply_fact_remove(self, p: dict[str, Any], turn: int) -> None:
        subject = _req_str(p, "subject")
        predicate = _req_str(p, "predicate")
        value = _req_str(p, "value")
        for index, fact in enumerate(self.facts):
            if (fact.subject, fact.predicate, fact.value) == (subject, predicate, value):
                del self.facts[index]
                return
        raise ValueError(f"fact not found: ({subject}, {predicate}, {value})")

    # -- story spine (M3: set once, then clamped deltas only — replay-safe) ------------

    def _apply_spine_set(self, p: dict[str, Any], turn: int) -> None:
        if self.spine is not None:
            raise ValueError("the spine is already set (spine_set is once per campaign)")
        raw = p.get("spine")
        if not isinstance(raw, dict):
            raise ValueError("spine_set requires a spine object")
        self.spine = StorySpine.from_dict(raw)

    def _apply_spine_delta(self, p: dict[str, Any], turn: int) -> None:
        if self.spine is None:
            raise ValueError("no spine to update (spine_set first)")
        op = _req_str(p, "op")
        if not op:
            raise ValueError("spine_delta requires an op")
        self.spine.apply_delta(op, p)

    # -- rolling campaign summary (M5: log-resident, replay-safe) ------------------

    def _apply_summary_set(self, p: dict[str, Any], turn: int) -> None:
        summary = _req_str(p, "summary")
        if not summary:
            raise ValueError("summary_set requires a non-empty summary")
        watermark = _req_int(p, "watermark")
        if watermark > turn:
            raise ValueError(f"summary watermark cannot be past its own turn: {watermark} > {turn}")
        self.summary = summary
        self.summary_watermark = watermark

    # -- combat (authoritative results — committed numbers, never re-rolled) ----------

    def _apply_combat_begin(self, p: dict[str, Any], turn: int) -> None:
        if self.combat is not None:
            raise ValueError("a combat is already in progress")
        raw_participants = _req_list(p, "participants")
        if len(raw_participants) < 2:
            raise ValueError("an encounter needs at least 2 combatants")
        participants: dict[str, CombatantRecord] = {}
        for raw in raw_participants:
            record = CombatantRecord(
                name=_req_str(raw, "name"),
                ac=_req_int(raw, "ac"),
                hp=_req_int(raw, "hp"),
                max_hp=_req_int(raw, "max_hp"),
                dexterity=_req_int(raw, "dexterity"),
                attack_bonus=_req_int(raw, "attack_bonus"),
                damage=_req_str(raw, "damage"),
                conditions=frozenset(_req_list(raw, "conditions", default=[])),
            )
            if record.name in participants:
                raise ValueError(f"duplicate combatant name: {record.name!r}")
            participants[record.name] = record
        if not any(record.hp > 0 for record in participants.values()):
            raise ValueError("no living combatant left to act")
        raw_order = _req_list(p, "order")
        if len(raw_order) != len(participants):
            raise ValueError("combat order must name exactly the participants")
        order: list[str] = []
        for entry in raw_order:
            if not isinstance(entry, dict):
                raise ValueError(f"combat order entry must be an object: {entry!r}")
            name = _req_str(entry, "name")
            if name not in participants:
                raise ValueError(f"combat order names a stranger: {name!r}")
            if name in order:
                raise ValueError(f"combat order names {name!r} twice")
            participants[name].initiative = _req_int(entry, "initiative")
            order.append(name)
        index = order.index(next(name for name in order if participants[name].hp > 0))
        self.combat = CombatState(participants=participants, order=order, index=index, round=0)

    def _apply_combat_attack(self, p: dict[str, Any], turn: int) -> None:
        self._require_combat()
        attacker = self.combatant(_req_str(p, "attacker"))
        defender = self.combatant(_req_str(p, "defender"))
        _req_bool(p, "melee")
        if not _req_bool(p, "hit"):
            return
        # M1 `can_act`: a 0-HP or stunned/unconscious creature cannot attack.
        if attacker.hp <= 0 or {"stunned", "unconscious"} & attacker.conditions:
            raise ValueError(f"attacker cannot act: {attacker.name!r}")
        if defender.is_dead:
            raise ValueError(f"cannot attack a dead combatant: {defender.name!r}")
        hp = _req_int(p, "defender_hp")
        successes = _req_int(p, "death_successes")
        failures = _req_int(p, "death_failures")
        _req_int(p, "damage")
        if not 0 <= hp <= defender.max_hp:
            raise ValueError(f"defender_hp out of range: {hp}")
        if not 0 <= successes <= 3 or not 0 <= failures <= 3:
            raise ValueError(f"death saves must be 0..3: successes={successes} failures={failures}")
        if hp > 0:
            # M1: positive hp clears death saves, except the natural-20
            # revive (back to exactly 1 HP with exactly one success kept).
            pair = (successes, failures)
            if pair not in ((0, 0), (1, 0)) or (pair == (1, 0) and hp != 1):
                detail = f"got successes={successes} failures={failures}"
                raise ValueError(f"hp {hp} allows no pending death saves: {detail}")
        if hp == 0 and not (successes or failures):
            raise ValueError("0 hp requires a committed death save")
        if hp == 0 and successes == 3 and failures == 3:
            raise ValueError("a combatant cannot be both stable and dead")
        defender.hp = hp
        defender.death_successes = successes
        defender.death_failures = failures

    def _apply_combat_condition(self, p: dict[str, Any], turn: int) -> None:
        self._require_combat()
        target = self.combatant(_req_str(p, "target"))
        condition = _req_str(p, "condition")
        if condition not in CONDITIONS:
            raise ValueError(f"unknown condition: {condition!r}")
        if _req_bool(p, "add"):
            target.conditions = target.conditions | {condition}
        else:
            target.conditions = target.conditions - {condition}

    def _apply_combat_turn(self, p: dict[str, Any], turn: int) -> None:
        """Pass the turn to the next combatant that can still act (M1 semantics).

        Only the current combatant may act; a combatant at 0 HP (or dead)
        is skipped; wrapping around starts a new round. Mirrors M1
        `Combat.end_turn` exactly — including the no-survivors error.
        """
        combat = self._require_combat()
        actor = _req_str(p, "actor")
        if actor != combat.current:
            raise ValueError(f"not {actor!r}'s turn: it is {combat.current!r}")
        if not any(record.hp > 0 for record in combat.participants.values()):
            raise ValueError("no living combatant left to act")
        index = (combat.index + 1) % len(combat.order)
        if index == 0:
            combat.round += 1
        while combat.participants[combat.order[index]].hp <= 0:
            index = (index + 1) % len(combat.order)
            if index == 0:
                combat.round += 1
        combat.index = index

    def _apply_combat_heal(self, p: dict[str, Any], turn: int) -> None:
        self._require_combat()
        target = self.combatant(_req_str(p, "target"))
        amount = _req_int(p, "amount")
        if amount < 0:
            raise ValueError(f"heal amount cannot be negative: {amount}")
        target.hp = min(target.max_hp, target.hp + amount)
        if target.hp > 0:
            target.death_successes = 0
            target.death_failures = 0

    def _apply_combat_end(self, p: dict[str, Any], turn: int) -> None:
        combat = self._require_combat()
        outcome = _req_str(p, "outcome")
        if outcome not in COMBAT_OUTCOMES:
            raise ValueError(f"combat outcome must be one of {COMBAT_OUTCOMES!r}: {outcome!r}")
        combat.outcome = outcome
        self.combat = None

    def _require_combat(self) -> CombatState:
        if self.combat is None:
            raise ValueError("no combat in progress")
        return self.combat

    # -- serialisation ---------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Canonical form: deterministic JSON (used for the fingerprint)."""
        sheet = self.pc.sheet if self.pc is not None else None
        return {
            "turn": self.turn,
            "summary": self.summary,
            "summary_watermark": self.summary_watermark,
            "pc": None
            if self.pc is None
            else {
                **sheet.__dict__,
                "location": self.pc.location,
                "description": self.pc.description,
                "notes": list(self.pc.notes),
            },
            "npcs": {
                name: {**npc.__dict__, "notes": list(npc.notes)}
                for name, npc in self.npcs.items()
            },
            "items": {name: {**item.__dict__} for name, item in self.items.items()},
            "quests": {name: {**quest.__dict__} for name, quest in self.quests.items()},
            "locations": {name: {**loc.__dict__} for name, loc in self.locations.items()},
            "facts": [
                {
                    "subject": f.subject,
                    "predicate": f.predicate,
                    "value": f.value,
                    "turn": f.turn,
                    "source": f.source,
                }
                for f in self.facts
            ],
            "combat": None
            if self.combat is None
            else {
                "participants": {
                    name: {**record.__dict__, "conditions": sorted(record.conditions)}
                    for name, record in self.combat.participants.items()
                },
                "order": list(self.combat.order),
                "index": self.combat.index,
                "round": self.combat.round,
                "outcome": self.combat.outcome,
            },
            "spine": None if self.spine is None else self.spine.to_dict(),
        }

    def fingerprint(self) -> str:
        """sha256 of the canonical JSON — names the world exactly."""
        canonical = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
