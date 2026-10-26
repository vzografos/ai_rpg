"""M2 property test: rebuild world from events alone == live DB (the state/memory layout).

Seeded random play. Each turn applies one valid operation — sheet mutations,
entity changes, facts, and combat results bridged through the *real* M1
`Combat` (the generator keeps a live M1 combat and emits its committed
results as event payloads) — committed through `WorldDB`. At the end, three
views of the same world must agree:

- A: the materialized entity tables (`load_from_tables`) — the SQL view
- B: the pure replay of the event log (`rebuild_world`) — the log view
- C: a restart (a fresh `WorldDB` on the same file; `load_world` verifies
  the replay against the last committed snapshot) — the process view

Persistent entities: A == B (projected). Full state, combat included:
B == C. Deterministic per seed; run several.

Combat candidates are lazy factories: the M1 bridge mutates only when its
candidate is actually chosen, so unchosen candidates can never desync the
bridge from the world.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from pathlib import Path

import pytest

from rpg.engine.combat import CONDITIONS, Combat, Combatant
from rpg.engine.dice import SeededSource
from rpg.engine.events import Message, WorldDB, WorldEvent, rebuild_world
from rpg.engine.sheet import ABILITIES
from rpg.engine.state import WorldState

TURNS = 40
SEEDS = (1, 7, 42, 1234, 99999)

WORLD_INIT = {
    "pc": {
        "name": "Mira",
        "hp": 14,
        "max_hp": 14,
        "ac": 14,
        "abilities": {ability: 10 for ability in ABILITIES},
        "inventory": {"Torch": 2, "Ration": 3},
        "gold": 25,
        "xp": 0,
    },
    "pc_location": "The village square",
    "locations": [
        {"name": "The village square", "description": "A quiet square.", "exits": ["The tavern"]},
        {
            "name": "The tavern",
            "description": "Woodsmoke and low voices.",
            "exits": ["The village square"],
        },
    ],
    "npcs": [
        {
            "name": "Brenna",
            "location": "The tavern",
            "description": "The tavern keeper.",
            "agenda": "She is hunting the shadow crown and will not say so.",
        }
    ],
    "quests": [
        {
            "name": "Find the shadow crown",
            "description": "The mayor pays for it.",
            "reward": "200 gp",
        }
    ],
    "items": [
        {"name": "Rusty key", "location": "The village square", "description": "Smells of iron."}
    ],
}

NPC_POOL = ["Old Fen", "Maro", "Sable", "Tamsin", "Vell", "Wren", "Yrsa"]
LOCATION_POOL = ["The crypt", "The market", "The cellar", "The ridge", "The chapel", "The dock"]
ITEM_POOL = [
    "Silver dagger",
    "Old map",
    "Healing draught",
    "Iron chain",
    "Glass vial",
    "Bone token",
    "Copper lamp",
]
QUEST_POOL = [
    "Guard the bridge",
    "Track the smugglers",
    "Cure the blight",
    "Escort the merchant",
    "Recover the seal",
    "Question the priest",
]
FOE_POOL = [
    {
        "name": "Grak",
        "ac": 12,
        "hp": 8,
        "max_hp": 8,
        "dexterity": 12,
        "attack_bonus": 3,
        "damage": "1d6+2",
    },
    {
        "name": "Thorn",
        "ac": 14,
        "hp": 12,
        "max_hp": 12,
        "dexterity": 10,
        "attack_bonus": 4,
        "damage": "1d8",
    },
    {
        "name": "Moss",
        "ac": 11,
        "hp": 6,
        "max_hp": 6,
        "dexterity": 14,
        "attack_bonus": 2,
        "damage": "1d4+1",
    },
    {
        "name": "Vex",
        "ac": 13,
        "hp": 10,
        "max_hp": 10,
        "dexterity": 12,
        "attack_bonus": 3,
        "damage": "1d6+1",
    },
]

Candidate = tuple[str, Callable[[], tuple[dict, list[WorldEvent]]]]


def _persistent_view(world: WorldState) -> dict:
    """The materialized-entity projection (combat lives only in the log)."""
    data = world.to_dict()
    return {
        key: data[key]
        for key in ("pc", "npcs", "items", "quests", "locations", "facts", "summary", "turn")
    }


class TurnForge:
    """Seeded random operation generator; the M1 `Combat` is the live bridge.

    Combat payloads are produced by the real M1 resolver (rolls, initiative,
    death saves), so the property test also pins that replaying committed
    results reproduces exactly what M1 did in-memory.
    """

    def __init__(self, seed: int) -> None:
        self.rng = random.Random(seed)
        self.turn = 1
        self._m1: Combat | None = None
        self._used_foes: set[str] = set()
        self._fact_counter = 0
        self._verbs = ["explore", "listen", "search", "climb", "follow", "ask", "signal", "wait"]
        self._outcomes = [
            "the dark answers back",
            "a door stands ajar",
            "dust stirs behind you",
            "the air turns cold",
            "something scratches",
        ]

    # -- the turn ----------------------------------------------------------

    def next_turn(self, world: WorldState) -> tuple[list[WorldEvent], list[Message]]:
        candidates = self._candidates(world)
        assert candidates, "the generator must always offer at least one valid operation"
        in_combat = self._m1 is not None and not self._m1.is_over
        if in_combat and self.rng.random() < 0.7:
            candidates = [c for c in candidates if c[0].startswith("combat")]
            assert candidates, "an active combat must offer a combat operation"
        kind, factory = self.rng.choice(candidates)
        payload, follow_ups = factory()
        events = [WorldEvent(turn=self.turn, kind=kind, payload=payload), *follow_ups]
        messages = [
            Message(turn=self.turn, role="user", content=f"I {self.rng.choice(self._verbs)}."),
            Message(
                turn=self.turn,
                role="narration",
                content=f"You act, and {self.rng.choice(self._outcomes)}.",
            ),
        ]
        self.turn += 1
        return events, messages

    # -- candidates --------------------------------------------------------

    @staticmethod
    def _const(kind: str, payload: dict) -> Candidate:
        def factory() -> tuple[dict, list[WorldEvent]]:
            return payload, []

        return (kind, factory)

    def _candidates(self, world: WorldState) -> list[Candidate]:
        candidates: list[Candidate] = []
        pc = world.pc
        if pc is not None:
            sheet = pc.sheet
            if sheet.hp > 0:
                candidates.append(
                    self._const("pc_damage", {"amount": self.rng.randint(1, sheet.hp)})
                )
            candidates.append(self._const("pc_heal", {"amount": self.rng.randint(1, 10)}))
            candidates.append(self._const("pc_gain_xp", {"amount": self.rng.randint(100, 500)}))
            candidates.append(self._const("pc_gold", {"amount": self.rng.randint(-sheet.gold, 20)}))
            candidates.append(
                self._const(
                    "pc_item_add",
                    {"name": self.rng.choice(ITEM_POOL), "quantity": self.rng.randint(1, 2)},
                )
            )
            if sheet.inventory:
                name = self.rng.choice(sorted(sheet.inventory))
                candidates.append(self._const("pc_item_remove", {"name": name, "quantity": 1}))
            others = [name for name in world.locations if name != pc.location]
            if others:
                candidates.append(self._const("pc_move", {"location": self.rng.choice(others)}))
            candidates.append(
                self._const(
                    "pc_note",
                    {
                        "text": self.rng.choice(
                            [
                                "The gate was locked.",
                                "A coin lay in the dust.",
                                "The wall was warm.",
                            ]
                        )
                    },
                )
            )
        for name in sorted(world.npcs):
            npc = world.npcs[name]
            candidates.append(
                self._const("npc_note", {"name": name, "text": "kept their counsel."})
            )
            if npc.status == "alive":
                candidates.append(self._const("npc_set_status", {"name": name, "status": "dead"}))
        if world.npcs and world.locations:
            name = self.rng.choice(sorted(world.npcs))
            candidates.append(
                self._const(
                    "npc_set_location",
                    {"name": name, "location": self.rng.choice(sorted(world.locations))},
                )
            )
        unused_npcs = [name for name in NPC_POOL if name not in world.npcs]
        if unused_npcs and world.locations:
            candidates.append(
                self._const(
                    "npc_add",
                    {
                        "name": self.rng.choice(unused_npcs),
                        "location": self.rng.choice(sorted(world.locations)),
                    },
                )
            )
        unused_locations = [name for name in LOCATION_POOL if name not in world.locations]
        if unused_locations:
            exits = self.rng.sample(
                sorted(world.locations), k=self.rng.randint(0, min(2, len(world.locations)))
            )
            candidates.append(
                self._const(
                    "location_add",
                    {
                        "name": self.rng.choice(unused_locations),
                        "description": "New ground.",
                        "exits": exits,
                    },
                )
            )
        unused_items = [name for name in ITEM_POOL if name not in world.items]
        if unused_items:
            holder = self._holder_name(world)
            location = (
                ""
                if holder
                else (self.rng.choice(sorted(world.locations)) if world.locations else "")
            )
            candidates.append(
                self._const(
                    "item_add",
                    {"name": self.rng.choice(unused_items), "holder": holder, "location": location},
                )
            )
        if world.items:
            name = self.rng.choice(sorted(world.items))
            candidates.append(
                self._const(
                    "item_set_holder",
                    {"name": name, "holder": self.rng.choice([self._holder_name(world), ""])},
                )
            )
        unused_quests = [name for name in QUEST_POOL if name not in world.quests]
        if unused_quests:
            candidates.append(
                self._const(
                    "quest_add",
                    {"name": self.rng.choice(unused_quests), "description": "A new errand."},
                )
            )
        for name in sorted(world.quests):
            quest = world.quests[name]
            if quest.status == "open":
                candidates.append(
                    self._const("quest_set_status", {"name": name, "status": "active"})
                )
            elif quest.status == "active":
                candidates.append(
                    self._const(
                        "quest_set_status",
                        {"name": name, "status": self.rng.choice(["done", "skipped"])},
                    )
                )
        self._fact_counter += 1
        candidates.append(
            self._const(
                "fact_add",
                {"subject": "Mira", "predicate": "noted", "value": f"detail-{self._fact_counter}"},
            )
        )
        if world.facts:
            fact = self.rng.choice(world.facts)
            candidates.append(
                self._const(
                    "fact_remove",
                    {"subject": fact.subject, "predicate": fact.predicate, "value": fact.value},
                )
            )
        if self._m1 is None:
            factory = self._combat_begin_factory(world)
            if factory is not None:
                candidates.append(("combat_begin", factory))
        else:
            for kind, factory in (
                ("combat_attack", self._combat_attack_factory(world)),
                ("combat_turn", self._combat_turn_factory(world)),
                ("combat_condition", self._combat_condition_factory(world)),
                ("combat_heal", self._combat_heal_factory(world)),
                ("combat_end", self._combat_end_factory(world)),
            ):
                if factory is not None:
                    candidates.append((kind, factory))
        return candidates

    def _holder_name(self, world: WorldState) -> str:
        if world.pc is not None and self.rng.random() < 0.5:
            return f"pc:{world.pc.sheet.name}"
        alive = [name for name, npc in world.npcs.items() if npc.status == "alive"]
        if alive:
            return f"npc:{self.rng.choice(alive)}"
        return ""

    # -- the M1 combat bridge (lazy: mutates only when chosen) ---------------

    def _combat_begin_factory(
        self, world: WorldState
    ) -> Callable[[], tuple[dict, list[WorldEvent]]] | None:
        if self._m1 is not None or not world.pc:
            return None
        remaining = [foe for foe in FOE_POOL if foe["name"] not in self._used_foes]
        if not remaining:
            return None
        sheet = world.pc.sheet
        participants = [
            {
                "name": sheet.name,
                "ac": sheet.ac,
                "hp": sheet.hp,
                "max_hp": sheet.max_hp,
                "dexterity": sheet.abilities["dexterity"],
                "attack_bonus": sheet.modifier("dexterity") + 2,
                "damage": "1d8+1",
            }
        ]

        def factory() -> tuple[dict, list[WorldEvent]]:
            combatants = [dict(participants[0])]
            for foe in self.rng.sample(remaining, k=self.rng.randint(1, min(2, len(remaining)))):
                self._used_foes.add(foe["name"])
                combatants.append(dict(foe))
            built = [
                Combatant(
                    name=data["name"],
                    ac=data["ac"],
                    hp=data["hp"],
                    max_hp=data["max_hp"],
                    dexterity=data["dexterity"],
                    attack_bonus=data["attack_bonus"],
                    damage=data["damage"],
                )
                for data in combatants
            ]
            self._m1 = Combat(SeededSource(self.rng.randint(0, 2**31)), built)
            order = self._m1.begin()
            payload = {
                "participants": [
                    {
                        "name": c.name,
                        "ac": c.ac,
                        "hp": c.hp,
                        "max_hp": c.max_hp,
                        "dexterity": c.dexterity,
                        "attack_bonus": c.attack_bonus,
                        "damage": c.damage,
                        "conditions": sorted(c.conditions),
                    }
                    for c in self._m1.participants
                ],
                "order": [{"name": c.name, "initiative": c.initiative} for c in order],
            }
            return payload, []

        return factory

    def _combat_attack_factory(
        self, world: WorldState
    ) -> Callable[[], tuple[dict, list[WorldEvent]]] | None:
        m1 = self._m1
        if m1 is None or m1.is_over:
            return None
        attacker = m1.current
        if not m1.can_act(attacker):
            return None
        defenders = [c for c in m1.participants if c.name != attacker.name and not c.is_dead]
        if not defenders:
            return None

        def factory() -> tuple[dict, list[WorldEvent]]:
            defender = self.rng.choice(defenders)
            melee = self.rng.random() < 0.8
            result = m1.attack(attacker, defender, melee=melee)
            payload = {
                "attacker": result.attacker,
                "defender": result.defender,
                "melee": melee,
                "hit": result.hit,
                "damage": result.damage,
                "defender_hp": defender.hp,
                "death_successes": defender.death_successes,
                "death_failures": defender.death_failures,
            }
            return payload, []

        return factory

    def _combat_turn_factory(
        self, world: WorldState
    ) -> Callable[[], tuple[dict, list[WorldEvent]]] | None:
        m1 = self._m1
        if m1 is None or m1.is_over:
            return None
        if not any(c.hp > 0 for c in m1.participants):
            return None
        actor = m1.current.name

        def factory() -> tuple[dict, list[WorldEvent]]:
            m1.end_turn()
            return {"actor": actor}, []

        return factory

    def _combat_condition_factory(
        self, world: WorldState
    ) -> Callable[[], tuple[dict, list[WorldEvent]]] | None:
        m1 = self._m1
        if m1 is None or m1.is_over:
            return None
        target = self.rng.choice(m1.participants)
        condition = self.rng.choice(CONDITIONS)
        add = self.rng.random() < 0.5

        def factory() -> tuple[dict, list[WorldEvent]]:
            if add:
                m1.add_condition(target, condition)
            else:
                m1.remove_condition(target, condition)
            return {"target": target.name, "condition": condition, "add": add}, []

        return factory

    def _combat_heal_factory(
        self, world: WorldState
    ) -> Callable[[], tuple[dict, list[WorldEvent]]] | None:
        m1 = self._m1
        if m1 is None or m1.is_over:
            return None
        target = self.rng.choice(m1.participants)
        amount = self.rng.randint(1, 10)

        def factory() -> tuple[dict, list[WorldEvent]]:
            m1.heal(target, amount)
            return {"target": target.name, "amount": amount}, []

        return factory

    def _combat_end_factory(
        self, world: WorldState
    ) -> Callable[[], tuple[dict, list[WorldEvent]]] | None:
        m1 = self._m1
        if m1 is None:
            return None
        outcome = self.rng.choice(["victory", "defeat", "fled"])
        pc_name = world.pc.sheet.name if world.pc else None

        def factory() -> tuple[dict, list[WorldEvent]]:
            self._m1 = None
            follow_ups: list[WorldEvent] = []
            if pc_name in {c.name for c in m1.participants}:
                # The encounter ended: the sheet catches up with the committed combat HP.
                pc_combatant = next(c for c in m1.participants if c.name == pc_name)
                follow_ups.append(
                    WorldEvent(turn=self.turn, kind="pc_set_hp", payload={"hp": pc_combatant.hp})
                )
            return {"outcome": outcome}, follow_ups

        return factory


# --- the property ----------------------------------------------------------------


@pytest.mark.parametrize("seed", SEEDS)
def test_rebuild_matches_live_db_across_restart(tmp_path: Path, seed: int) -> None:
    db_path = tmp_path / f"prop-{seed}.db"
    forge = TurnForge(seed)
    with WorldDB(db_path) as db:
        world = WorldState()
        world.apply_event("world_init", WORLD_INIT, turn=0)
        db.commit(world, [WorldEvent(turn=0, kind="world_init", payload=WORLD_INIT)], [])
        assert world.turn == 1
        for _ in range(TURNS):
            events, messages = forge.next_turn(world)
            for event in events:
                world.apply_event(event.kind, event.payload, event.turn)
            db.commit(world, events, messages)
        live_full = world.to_dict()
        live_persistent = _persistent_view(world)
        live_hash = world.fingerprint()

    # B: the pure replay of the event log (the M2 authority).
    with WorldDB(db_path) as db:
        replayed = rebuild_world(db.events())
        replayed.turn = world.turn
        assert replayed.fingerprint() == live_hash, (
            "replay of the event log must equal the live world"
        )
        assert replayed.to_dict() == live_full

        # A: the materialized entity tables (the SQL view).
        tables = db.load_from_tables()
        assert _persistent_view(tables) == live_persistent, (
            "materialized tables must equal the replay's persistent entities"
        )

    # C: the restart — a fresh process on the same file (snapshot-verified).
    with WorldDB(db_path) as db:
        restarted = db.load_world()
        assert restarted is not None
        assert restarted.fingerprint() == live_hash, (
            "kill + restart must reproduce the identical world"
        )
        assert restarted.to_dict() == live_full
        # The materialized view resynced by the load agrees on the persistent entities.
        assert _persistent_view(db.load_from_tables()) == live_persistent


@pytest.mark.parametrize("seed", SEEDS)
def test_every_turn_was_named_by_a_snapshot(tmp_path: Path, seed: int) -> None:
    """Each committed turn carries a snapshot naming the world at that turn."""
    db_path = tmp_path / f"snap-{seed}.db"
    forge = TurnForge(seed)
    with WorldDB(db_path) as db:
        world = WorldState()
        world.apply_event("world_init", WORLD_INIT, turn=0)
        db.commit(world, [WorldEvent(turn=0, kind="world_init", payload=WORLD_INIT)], [])
        for _ in range(12):
            events, messages = forge.next_turn(world)
            for event in events:
                world.apply_event(event.kind, event.payload, event.turn)
            db.commit(world, events, messages)
        hashes = {turn: db.snapshot_for(turn) for turn in range(world.turn)}
    assert all(hashes.values()), "every committed turn must be named by a snapshot"
    # A no-op turn (a missed attack) may legitimately re-name the same state;
    # the guarantee is presence, not distinctness.
