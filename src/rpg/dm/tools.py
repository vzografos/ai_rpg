"""DM tool schemas (roadmap M3): the only door by which the LLM touches state.

Tools (each dispatched to the engine, validated, clamped, logged):
    roll_check, spawn_npc, move_to, start_combat, end_combat,
    grant_item, take_item, quest_update, npc_note, note, ask_clarification
    + award (M3 extension: XP/gold are the table's numbers — "scoring falls
      out of the engine", so the DM needs a validated door to hand them out)

The engine is the only writer of numbers: a tool call that fails validation
is rejected, and the failure is fed back to the model as the tool result —
the model never gets to narrate around it. Combat is resolved by the engine
end to end (M1 `Combat`, seeded); the DM narrates from the authoritative
event strings the resolution produces.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from rpg.engine.combat import Combat, Combatant
from rpg.engine.dice import RandomSource
from rpg.engine.rules import ability_save
from rpg.engine.sheet import ABILITIES, CharacterSheet
from rpg.engine.state import WorldState

__all__ = [
    "MAX_COMBAT_ROUNDS",
    "DM_TOOLS",
    "ToolResult",
    "ToolEngine",
]

MAX_COMBAT_ROUNDS = 20  # a played encounter must terminate (M1: a seed can cycle)


@dataclass
class ToolResult:
    """One dispatched tool call: the model's feedback plus the turn's bookkeeping.

    `events` are (kind, payload) pairs already applied to the live world via
    `WorldState.apply_event`; the loop commits them with the turn. `dice` are
    player-facing dice frames; `authoritative` the event strings the
    narration must reflect verbatim (step 5: adjudication).
    """

    name: str
    ok: bool
    message: str
    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    dice: list[dict[str, Any]] = field(default_factory=list)
    authoritative: list[str] = field(default_factory=list)
    ends_turn: bool = False


def _pc_name(world: WorldState) -> str:
    if world.pc is None:
        raise ValueError("no player character in this world")
    return world.pc.sheet.name


def _holder_spec(world: WorldState, holder: str) -> str:
    """'pc' | 'npc:<name>' | '' -> the Item holder form (validated)."""
    if holder == "pc":
        return f"pc:{_pc_name(world)}"
    if holder == "":
        return ""
    if holder.startswith("npc:"):
        world.npc(holder[4:])
        return holder
    raise ValueError(f"holder must be 'pc', 'npc:<name>', or '' — got {holder!r}")


def _pc_melee_damage(sheet: CharacterSheet) -> str:
    """The PC's melee damage (shortsword 1d8 + Strength modifier)."""
    mod = sheet.modifier("strength")
    if mod > 0:
        return f"1d8+{mod}"
    if mod < 0:
        return f"1d8{mod}"
    return "1d8"


def _combatant(combat: Combat, name: str) -> Combatant:
    for candidate in combat.participants:
        if candidate.name == name:
            return candidate
    raise ValueError(f"not in this combat: {name!r}")


def _is_down(combat: Combat, name: str) -> bool:
    """Out of the fight: 0 HP (unconscious, stable, or dead)."""
    candidate = _combatant(combat, name)
    return candidate.hp == 0


# --- the schemas (the contract the model sees) ---------------------------------

DM_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "roll_check",
            "description": (
                "Resolve an ability check for the player character: the engine rolls "
                "d20 + the sheet's save bonus against the DC. Call this BEFORE narrating "
                "any uncertain outcome — a natural 20 always succeeds, a natural 1 always fails."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ability": {"type": "string", "enum": list(ABILITIES)},
                    "dc": {"type": "integer", "minimum": 1, "maximum": 30},
                    "advantage": {"type": "boolean", "default": False},
                    "disadvantage": {"type": "boolean", "default": False},
                    "note": {
                        "type": "string",
                        "description": "what the check is for (context only)",
                    },
                },
                "required": ["ability", "dc"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "spawn_npc",
            "description": (
                "Introduce an NPC into the world (name, where they stand, what they look "
                "like). The agenda is keeper-only: it is tracked, never revealed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "location": {
                        "type": "string",
                        "description": "a known location, or '' if unlocated",
                    },
                    "agenda": {"type": "string", "description": "keeper-only: what the NPC wants"},
                    "status": {"type": "string", "enum": ["alive", "dead"], "default": "alive"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_to",
            "description": "Move the player character to a known location (or move an NPC).",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {"type": "string"},
                    "npc": {"type": "string", "description": "set instead of the PC when given"},
                },
                "required": ["location"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "start_combat",
            "description": (
                "Start and resolve an encounter. The engine rolls initiative and every "
                "attack to completion; you narrate the rounds from the authoritative "
                "results it returns. Pass the monsters' stat blocks (use the adventure's "
                "encounter blocks); the PC is added automatically."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "participants": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "ac": {"type": "integer", "minimum": 1},
                                "hp": {"type": "integer", "minimum": 0},
                                "max_hp": {"type": "integer", "minimum": 1},
                                "dexterity": {"type": "integer", "minimum": 3, "maximum": 30},
                                "attack_bonus": {"type": "integer"},
                            "damage": {
                                "type": "string",
                                "description": "dice formula, e.g. 1d8+2",
                            },
                            "conditions": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": [
                            "name", "ac", "hp", "max_hp", "dexterity", "attack_bonus", "damage"
                        ],
                        },
                    },
                    "pc_target": {
                        "type": "string",
                        "description": "the monster the PC attacks first (default: first listed)",
                    },
                },
                "required": ["participants"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "end_combat",
            "description": (
                "Close the current encounter. The engine derives the outcome from the "
                "committed state (victory: all foes down; defeat: the PC falls) and will "
                "reject an outcome it does not agree with; 'fled' is always allowed. "
                "On victory, pass xp_award for the spoils (the engine records it)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "outcome": {"type": "string", "enum": ["victory", "defeat", "fled"]},
                    "xp_award": {"type": "integer", "minimum": 0, "default": 0},
                },
                "required": ["outcome"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rest",
            "description": (
                "Rest the player character: a long rest fully restores HP and ordinary "
                "spell slots; a short rest restores half the missing HP (rounded up), "
                "and restores warlock Pact Magic slots. The engine validates and "
                "records these changes; narration must reflect them verbatim."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["short", "long"], "default": "long"}
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grant_item",
            "description": (
                "Grant a new item to the PC or place it at a location. To give an item to "
                "an NPC, it must already exist and be held by the PC (or be in the PC's "
                "character-sheet inventory); the transfer removes it from the PC. Never "
                "create a new item directly in an NPC's keeping. If the item already "
                "exists in the world, it moves to the requested holder/location."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "holder": {"type": "string", "default": "pc"},
                    "quantity": {"type": "integer", "minimum": 1, "default": 1},
                    "description": {"type": "string"},
                    "location": {"type": "string", "description": "required when holder is ''"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "take_item",
            "description": (
                "Take a loose item from the world into the PC's keeping (it must exist and "
                "not be in someone's keeping — have them hand it over first, e.g. via "
                "grant_item with holder 'pc')."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "holder": {
                        "type": "string",
                        "description": (
                            "where it currently is (pc / npc:<name> / a location), "
                            "if not already with the PC"
                        ),
                    },
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "quest_update",
            "description": (
                "Create (if new) or advance a tracked quest. Status moves one way: "
                "open -> active -> done/skipped (done is final)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "status": {"type": "string", "enum": ["open", "active", "done", "skipped"]},
                    "description": {"type": "string"},
                    "reward": {"type": "string"},
                },
                "required": ["name", "status"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "npc_note",
            "description": "Record a keeper-only note on an NPC (how their story is going).",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "text": {"type": "string"}},
                "required": ["name", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "note",
            "description": (
                "Record a fact about what just happened (kept for the long memory). "
                "Use predicate 'spoke_with' for an NPC met, 'defeated' for a foe killed, "
                "'completed' for an objective met, 'occurred' for an event. If the fact "
                "advances the story, the value must match the active beat's waypoint text "
                "exactly — that is how the story spine ticks."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "subject": {"type": "string", "description": "default: the PC"},
                    "predicate": {"type": "string", "default": "noted"},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_clarification",
            "description": (
                "Ask the player a question and end your turn on it (the question is "
                "delivered as your narration). Use when the player's intent is genuinely "
                "ambiguous — not for outcomes."
            ),
            "parameters": {
                "type": "object",
                "properties": {"question": {"type": "string"}},
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "award",
            "description": (
                "Hand out the table's scoring: XP (positive) and gold (negative spends "
                "it). The engine clamps and records; narration must reflect it verbatim."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "xp": {"type": "integer", "minimum": 0, "default": 0},
                    "gold": {"type": "integer", "default": 0},
                },
            },
        },
    },
]


# --- the dispatch layer ----------------------------------------------------------


class ToolEngine:
    """One turn's tool dispatch: every call lands on the engine, validated.

    Holds the turn's applied events (`events`) and authoritative strings
    (`authoritative`); a rejected call returns ok=False and its reason as
    the model's tool result — nothing is applied that the engine did not accept.
    """

    def __init__(self, world: WorldState, source: RandomSource) -> None:
        self.world = world
        self.source = source
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.authoritative: list[str] = []

    def begin_turn(self) -> None:
        """One tool engine per session, but the turn's records are per turn."""
        self.events = []
        self.authoritative = []

    def dispatch(self, name: str, args: dict[str, Any]) -> ToolResult:
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            return ToolResult(name=name, ok=False, message=f"REJECTED: unknown tool {name!r}.")
        try:
            result = handler(args)
        except ValueError as exc:
            return ToolResult(name=name, ok=False, message=f"REJECTED: {exc}")
        # The turn's authoritative record (the M4 backstop + fallback render
        # from it): every accepted call's engine wording, in order.
        self.authoritative.extend(result.authoritative)
        return result

    def _apply(self, kind: str, payload: dict[str, Any]) -> None:
        self.world.apply_event(kind, payload, self.world.turn)
        self.events.append((kind, payload))

    # -- checks ----------------------------------------------------------------

    def _tool_roll_check(self, args: dict[str, Any]) -> ToolResult:
        ability = args.get("ability")
        dc = args.get("dc")
        if not isinstance(ability, str) or ability not in ABILITIES:
            raise ValueError(f"ability must be one of {list(ABILITIES)!r}: {ability!r}")
        if not isinstance(dc, int) or isinstance(dc, bool) or not 1 <= dc <= 30:
            raise ValueError(f"dc must be an int 1..30: {dc!r}")
        advantage = bool(args.get("advantage", False))
        disadvantage = bool(args.get("disadvantage", False))
        name = _pc_name(self.world)
        result = ability_save(
            self.source,
            self.world.pc.sheet,
            ability,
            dc,
            advantage=advantage,
            disadvantage=disadvantage,
        )
        if advantage and not disadvantage:
            formula = "2d20kh1"
        elif disadvantage and not advantage:
            formula = "2d20kl1"
        else:
            formula = "1d20"
        verdict = "SUCCESS" if result.success else "FAILURE"
        if result.critical:
            verdict += " (critical)"
        if result.fumble:
            verdict += " (fumble)"
        line = (
            f"{name} rolls {ability} vs DC {dc}: d20 {result.d20} + {result.modifier} "
            f"= {result.total} — {verdict}."
        )
        note = args.get("note", "")
        if isinstance(note, str) and note:
            line += f" ({note})"
        return ToolResult(
            name="roll_check",
            ok=True,
            message=f"Rolled {ability} vs DC {dc}: {result.total} — {verdict.lower()}.",
            dice=[
                {
                    "label": f"{ability} check vs DC {dc}",
                    "formula": formula,
                    "d20": result.d20,
                    "total": result.total,
                    "dc": dc,
                    "success": result.success,
                    "critical": result.critical,
                    "fumble": result.fumble,
                }
            ],
            authoritative=[line],
        )

    # -- world: npcs / movement ---------------------------------------------------

    def _tool_spawn_npc(self, args: dict[str, Any]) -> ToolResult:
        name = args.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("npc name must be a non-empty string")
        if name in self.world.npcs:
            raise ValueError(f"npc already exists: {name!r}")
        location = args.get("location", "")
        if not isinstance(location, str):
            raise ValueError("location must be a string")
        if location:
            self.world.location(location)
        status = args.get("status", "alive")
        if status not in ("alive", "dead"):
            raise ValueError(f"npc status must be alive|dead: {status!r}")
        self._apply(
            "npc_add",
            {
                "name": name,
                "location": location,
                "description": args.get("description", ""),
                "agenda": args.get("agenda", ""),
                "status": status,
            },
        )
        where = f" in {location}" if location else ""
        return ToolResult(
            name="spawn_npc",
            ok=True,
            message=f"{name} is now in the world{where}.",
        )

    def _tool_move_to(self, args: dict[str, Any]) -> ToolResult:
        location = args.get("location")
        if not isinstance(location, str) or not location:
            raise ValueError("location must be a non-empty string")
        self.world.location(location)
        npc_name = args.get("npc", "")
        if npc_name:
            if not isinstance(npc_name, str):
                raise ValueError("npc must be a string")
            self.world.npc(npc_name)
            self._apply("npc_set_location", {"name": npc_name, "location": location})
            return ToolResult(
                name="move_to", ok=True, message=f"{npc_name} moves to {location}."
            )
        self._apply("pc_move", {"location": location})
        return ToolResult(name="move_to", ok=True, message=f"You move to {location}.")

    # -- items ------------------------------------------------------------------

    def _tool_grant_item(self, args: dict[str, Any]) -> ToolResult:
        name = args.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("item name must be a non-empty string")
        raw_holder = args.get("holder")
        location = args.get("location", "")
        if not isinstance(location, str):
            raise ValueError("location must be a string")
        if raw_holder is not None and raw_holder != "" and location:
            raise ValueError(f"grant_item takes a holder or a location, not both: {name!r}")
        holder = _holder_spec(self.world, raw_holder or ("pc" if not location else ""))
        quantity = args.get("quantity", 1)
        if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 1:
            raise ValueError(f"quantity must be an int >= 1: {quantity!r}")
        description = args.get("description", "")
        if not isinstance(description, str):
            raise ValueError("description must be a string")
        pc_holder = f"pc:{_pc_name(self.world)}"
        pc_inventory = self.world.pc.sheet.inventory
        owned = pc_inventory.get(name, 0)
        existing = self.world.items.get(name)
        if existing is not None:
            if not holder:
                raise ValueError(
                    f"the {name!r} is already in the world — move it with a holder, not a location"
                )
            if existing.holder == holder:
                raise ValueError(f"the {name!r} is already with {holder}")
            if existing.holder == pc_holder and owned:
                raise ValueError(
                    f"the {name!r} is represented both as a world item and in the PC's inventory"
                )
            if holder.startswith("npc:") and existing.holder != pc_holder:
                raise ValueError(
                    f"you cannot give the {name!r} to {holder}: it is not held by the PC"
                )
            if holder == pc_holder and owned:
                raise ValueError(f"the {name!r} is already in the PC's character-sheet inventory")
            previous_holder = existing.holder
            previous_location = existing.location
            self._apply("item_set_holder", {"name": name, "holder": holder})
            if holder.startswith("npc:"):
                npc_name = holder[4:]
                line = f"You give the {name} to {npc_name}."
            elif holder == pc_holder and previous_holder.startswith("npc:"):
                line = f"{previous_holder[4:]} hands you the {name}."
            elif holder == pc_holder and previous_location:
                line = f"You take the {name} from {previous_location}."
            elif holder == pc_holder:
                line = f"You receive the {name}."
            else:
                line = f"The {name} is now with {holder}."
            return ToolResult(
                name="grant_item",
                ok=True,
                message=line,
                authoritative=[line],
            )
        if not holder and not location:
            raise ValueError(f"grant_item needs a holder or a location: {name!r}")
        if holder.startswith("npc:"):
            if owned < quantity:
                raise ValueError(
                    f"cannot give {quantity}x {name!r} to {holder}: "
                    f"the PC only has {owned} in their inventory"
                )
            self._apply("pc_item_remove", {"name": name, "quantity": quantity})
            self._apply(
                "item_add",
                {
                    "name": name,
                    "holder": holder,
                    "location": "",
                    "quantity": quantity,
                    "description": description,
                },
            )
            npc_name = holder[4:]
            line = f"You give {quantity} {name} to {npc_name}."
            return ToolResult(
                name="grant_item",
                ok=True,
                message=line,
                authoritative=[line],
            )
        if owned and holder == pc_holder:
            raise ValueError(f"the {name!r} is already in the PC's character-sheet inventory")
        if owned and location:
            if owned < quantity:
                raise ValueError(
                    f"cannot place {quantity}x {name!r}: the PC only has {owned}"
                )
            self._apply("pc_item_remove", {"name": name, "quantity": quantity})
        self._apply(
            "item_add",
            {
                "name": name,
                "holder": holder,
                "location": location,
                "quantity": quantity,
                "description": description,
            },
        )
        if holder == pc_holder:
            line = f"You receive the {name}."
        elif location:
            line = f"You place the {name} at {location}."
        else:
            line = f"The {name} is now with {holder}."
        return ToolResult(
            name="grant_item",
            ok=True,
            message=line,
            authoritative=[line],
        )

    def _tool_take_item(self, args: dict[str, Any]) -> ToolResult:
        name = args.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("item name must be a non-empty string")
        item = self.world.item(name)
        pc_holder = f"pc:{_pc_name(self.world)}"
        if item.holder == pc_holder:
            raise ValueError(f"the {name!r} is already yours")
        holder = args.get("holder", "")
        if not isinstance(holder, str):
            raise ValueError("holder must be a string")
        if not holder and item.holder:
            raise ValueError(f"the {name!r} is with {item.holder} — have them hand it over first")
        if holder:
            current = item.holder if item.holder else f"location:{item.location}"
            if holder in ("pc", pc_holder):
                if item.holder:
                    raise ValueError(f"the {name!r} is with {item.holder}, not the PC")
            elif holder.startswith("npc:"):
                if item.holder != holder:
                    raise ValueError(f"the {name!r} is {current}, not with {holder}")
            else:
                if item.holder or item.location != holder:
                    raise ValueError(f"the {name!r} is {current}, not at {holder!r}")
        self._apply("item_set_holder", {"name": name, "holder": pc_holder})
        line = f"You take the {name}."
        return ToolResult(
            name="take_item", ok=True, message=line, authoritative=[line]
        )

    # -- quests / notes ----------------------------------------------------------

    def _tool_quest_update(self, args: dict[str, Any]) -> ToolResult:
        name = args.get("name")
        status = args.get("status")
        if not isinstance(name, str) or not name:
            raise ValueError("quest name must be a non-empty string")
        if status not in ("open", "active", "done", "skipped"):
            raise ValueError(f"quest status must be open|active|done|skipped: {status!r}")
        description = args.get("description", "")
        reward = args.get("reward", "")
        if not isinstance(description, str) or not isinstance(reward, str):
            raise ValueError("description/reward must be strings")
        if name in self.world.quests:
            self._apply("quest_set_status", {"name": name, "status": status})
        else:
            self._apply(
                "quest_add",
                {"name": name, "description": description, "status": status, "reward": reward},
            )
        return ToolResult(name="quest_update", ok=True, message=f"Quest {name!r} is now {status}.")

    def _tool_npc_note(self, args: dict[str, Any]) -> ToolResult:
        name = args.get("name")
        text = args.get("text")
        if not isinstance(name, str) or not name:
            raise ValueError("npc name must be a non-empty string")
        if not isinstance(text, str) or not text:
            raise ValueError("note text must be a non-empty string")
        self.world.npc(name)
        self._apply("npc_note", {"name": name, "text": text})
        return ToolResult(name="npc_note", ok=True, message=f"Noted for {name}.")

    def _tool_note(self, args: dict[str, Any]) -> ToolResult:
        text = args.get("text")
        if not isinstance(text, str) or not text:
            raise ValueError("note text must be a non-empty string")
        subject = args.get("subject") or _pc_name(self.world)
        predicate = args.get("predicate") or "noted"
        if not isinstance(subject, str) or not subject:
            raise ValueError("subject must be a non-empty string")
        if not isinstance(predicate, str) or not predicate:
            raise ValueError("predicate must be a non-empty string")
        self._apply(
            "fact_add",
            {"subject": subject, "predicate": predicate, "value": text, "source": "dm"},
        )
        line = f"Recorded: {subject} {predicate} {text}."
        return ToolResult(
            name="note",
            ok=True,
            message=line,
            authoritative=[line],
        )

    # -- the player's turn --------------------------------------------------------

    def _tool_ask_clarification(self, args: dict[str, Any]) -> ToolResult:
        question = args.get("question")
        if not isinstance(question, str) or not question:
            raise ValueError("question must be a non-empty string")
        return ToolResult(
            name="ask_clarification",
            ok=True,
            message=question,
            ends_turn=True,
        )

    def _tool_award(self, args: dict[str, Any]) -> ToolResult:
        xp = args.get("xp", 0)
        gold = args.get("gold", 0)
        if not isinstance(xp, int) or isinstance(xp, bool) or xp < 0:
            raise ValueError(f"xp must be an int >= 0: {xp!r}")
        if not isinstance(gold, int) or isinstance(gold, bool):
            raise ValueError(f"gold must be an int: {gold!r}")
        if xp == 0 and gold == 0:
            raise ValueError("award nothing to do: pass xp and/or gold")
        sheet = self.world.pc.sheet
        lines = []
        if xp:
            self._apply("pc_gain_xp", {"amount": xp})
            lines.append(f"You gain {xp} XP (total {sheet.xp}, level {sheet.level}).")
        if gold:
            self._apply("pc_gold", {"amount": gold})
            verb = "gain" if gold > 0 else "spend"
            lines.append(f"You {verb} {abs(gold)} gold (total {sheet.gold}).")
        return ToolResult(
            name="award", ok=True, message="Awarded.", authoritative=lines
        )

    def _tool_rest(self, args: dict[str, Any]) -> ToolResult:
        kind = args.get("kind", "long")
        if kind not in ("short", "long"):
            raise ValueError(f"kind must be short|long: {kind!r}")
        sheet = self.world.pc.sheet
        hp_before = sheet.hp
        slots_before = sheet.spell_slots
        if hp_before == sheet.max_hp and not sheet.can_restore_spell_slots(kind):
            raise ValueError("already at full HP with no spell slots to recover — nothing to rest")
        self._apply("pc_rest", {"kind": kind})
        healed = sheet.hp - hp_before
        restored = {
            slot_level: sheet.spell_slots[slot_level] - slots_before[slot_level]
            for slot_level in slots_before
            if sheet.spell_slots[slot_level] > slots_before[slot_level]
        }
        line = f"You rest and recover {healed} HP (now {sheet.hp}/{sheet.max_hp})."
        if restored:
            slots = ", ".join(
                f"{count} level {slot_level} spell slot{'s' if count != 1 else ''}"
                for slot_level, count in sorted(restored.items())
            )
            line += f" You recover {slots}."
        return ToolResult(name="rest", ok=True, message=line, authoritative=[line])

    # -- combat -------------------------------------------------------------------

    def _pc_combatant(self) -> Combatant:
        """The PC as an M1 combatant (the engine builds every number)."""
        sheet = self.world.pc.sheet
        return Combatant(
            name=sheet.name,
            ac=sheet.ac,
            hp=sheet.hp,
            max_hp=sheet.max_hp,
            dexterity=sheet.abilities["dexterity"],
            attack_bonus=sheet.save_bonus("strength"),
            damage=_pc_melee_damage(sheet),
        )

    @staticmethod
    def _monster_combatant(spec: dict[str, Any]) -> Combatant:
        if not isinstance(spec, dict):
            raise ValueError(f"combatant must be an object: {spec!r}")
        name = spec.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("combatant name must be a non-empty string")
        hp = spec.get("hp")
        max_hp = spec.get("max_hp")
        if not isinstance(hp, int) or isinstance(hp, bool) or hp < 0:
            raise ValueError(f"combatant hp must be an int >= 0: {hp!r}")
        if not isinstance(max_hp, int) or isinstance(max_hp, bool) or max_hp < 1:
            raise ValueError(f"combatant max_hp must be an int >= 1: {max_hp!r}")
        if hp > max_hp:
            raise ValueError(f"combatant hp {hp} exceeds max_hp {max_hp}")
        ac = spec.get("ac")
        if not isinstance(ac, int) or isinstance(ac, bool) or ac < 1:
            raise ValueError(f"combatant ac must be an int >= 1: {ac!r}")
        dexterity = spec.get("dexterity")
        if (
            not isinstance(dexterity, int)
            or isinstance(dexterity, bool)
            or not 3 <= dexterity <= 30
        ):
            raise ValueError(f"combatant dexterity must be 3..30: {dexterity!r}")
        attack_bonus = spec.get("attack_bonus")
        if not isinstance(attack_bonus, int) or isinstance(attack_bonus, bool):
            raise ValueError(f"combatant attack_bonus must be an int: {attack_bonus!r}")
        damage = spec.get("damage")
        if not isinstance(damage, str) or not damage:
            raise ValueError("combatant damage must be a non-empty dice formula")
        conditions = spec.get("conditions", [])
        if not isinstance(conditions, list) or not all(
            isinstance(condition, str) for condition in conditions
        ):
            raise ValueError("combatant conditions must be a list of strings")
        return Combatant(
            name=name,
            ac=ac,
            hp=hp,
            max_hp=max_hp,
            dexterity=dexterity,
            attack_bonus=attack_bonus,
            damage=damage,
            conditions=set(conditions),
        )

    def _attack_frames(self, result: Any, attacker: Combatant) -> list[dict[str, Any]]:
        """Player-facing dice frames for one resolved attack (step 9: render)."""
        if result.advantage and not result.disadvantage:
            to_hit_formula = "2d20kh1"
        elif result.disadvantage and not result.advantage:
            to_hit_formula = "2d20kl1"
        else:
            to_hit_formula = "1d20"
        frames = [
            {
                "label": f"{result.attacker} attacks {result.defender}",
                "formula": to_hit_formula,
                "d20": result.d20,
                "total": result.to_hit,
                "ac": result.ac,
                "hit": result.hit,
                "critical": result.critical,
            }
        ]
        if result.hit:
            frames.append(
                {
                    "label": f"{result.attacker} damage",
                    "formula": attacker.damage,
                    "total": result.damage,
                }
            )
            outcome = result.outcome
            if outcome is not None and outcome.death_save is not None:
                frames.append(
                    {
                        "label": f"{result.defender} death save",
                        "formula": "1d20",
                        "d20": outcome.death_save,
                        "success": outcome.death_save_success,
                    }
                )
        return frames

    def _attack_line(self, result: Any) -> str:
        """The authoritative event string the narration must reflect (step 5)."""
        if not result.hit:
            prefix = "fumble: " if result.fumble else ""
            return (
                f"{prefix}{result.attacker} misses {result.defender} "
                f"(to-hit {result.to_hit} vs AC {result.ac})."
            )
        outcome = result.outcome
        prefix = "critical: " if result.critical else ""
        line = f"{prefix}{result.attacker} hits {result.defender} for {result.damage}."
        if outcome is not None:
            if outcome.dead:
                line += f" {result.defender} dies."
            elif outcome.stable:
                line += f" {result.defender} is stable."
            elif outcome.hp == 0:
                line += (
                    f" {result.defender} falls unconscious"
                    f" (death save {outcome.death_save}: "
                    f"{'success' if outcome.death_save_success else 'failure'})."
                )
        return line

    def _tool_start_combat(self, args: dict[str, Any]) -> ToolResult:
        if self.world.combat is not None:
            raise ValueError("a combat is already in progress — close it with end_combat")
        specs = args.get("participants")
        if not isinstance(specs, list) or not specs:
            raise ValueError("participants must be a non-empty list of stat blocks")
        pc = self._pc_combatant()
        monsters = [self._monster_combatant(spec) for spec in specs]
        names = {c.name for c in [pc, *monsters]}
        if len(names) != len([pc, *monsters]):
            raise ValueError("combatant names must be unique")
        pc_target = args.get("pc_target", "")
        if pc_target and pc_target not in {m.name for m in monsters}:
            raise ValueError(f"pc_target {pc_target!r} is not a participant")
        combat = Combat(self.source, [pc, *monsters])
        order = combat.begin()
        self._apply(
            "combat_begin",
            {
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
                    for c in [pc, *monsters]
                ],
                "order": [{"name": c.name, "initiative": c.initiative} for c in order],
            },
        )
        frames = [
            {
                "label": f"initiative: {c.name}",
                "formula": "1d20",
                "total": c.initiative,
            }
            for c in order
        ]
        lines = ["Initiative: " + ", ".join(f"{c.name} ({c.initiative})" for c in order)]
        resolved = self._resolve_combat(combat, pc, monsters, pc_target, frames)
        lines += [self._attack_line(r) for r in resolved]
        if combat.is_over or pc.is_dead:
            derived = "defeat" if pc.is_dead else "victory"
            lines.append(f"The encounter is over: {derived}. Close it with end_combat.")
            return ToolResult(
                name="start_combat", ok=True, message=lines[-1], dice=frames, authoritative=lines
            )
        lines.append(
            f"The encounter is unresolved after {MAX_COMBAT_ROUNDS} rounds — "
            "close it with end_combat (outcome 'fled')."
        )
        return ToolResult(
            name="start_combat",
            ok=True,
            message=lines[-1],
            dice=frames,
            authoritative=lines,
        )

    def _resolve_combat(
        self,
        combat: Combat,
        pc: Combatant,
        monsters: list[Combatant],
        pc_target: str,
        frames: list[dict[str, Any]],
    ) -> list[Any]:
        """Run the encounter to completion (or the round cap), committing each
        attack and turn as it happens — the M2 property test's bridge, live."""
        results = []
        for _round in range(MAX_COMBAT_ROUNDS):
            if combat.is_over or pc.is_dead:
                break
            actor = combat.current
            if actor.name == pc.name:
                if pc_target and not _is_down(combat, pc_target):
                    target = _combatant(combat, pc_target)
                else:
                    target = next(
                        (m for m in monsters if not _is_down(combat, m.name)),
                        next(m for m in monsters),
                    )
            else:
                target = pc
            if not combat.can_act(actor):
                self._commit_turn(combat, actor.name)
                continue
            result = combat.attack(actor, target, melee=True)
            results.append(result)
            frames.extend(self._attack_frames(result, actor))
            self._commit_attack(combat, result)
            if combat.is_over or pc.is_dead:
                break
            self._commit_turn(combat, actor.name)
        return results

    def _commit_attack(self, combat: Combat, result: Any) -> None:
        defender = _combatant(combat, result.defender)
        self._apply(
            "combat_attack",
            {
                "attacker": result.attacker,
                "defender": result.defender,
                "melee": True,
                "hit": result.hit,
                "damage": result.damage,
                "defender_hp": defender.hp,
                "death_successes": defender.death_successes,
                "death_failures": defender.death_failures,
            },
        )
        # The turn's authoritative lines ride on the start_combat result
        # (dispatch collects them); no append here or they would double.

    def _commit_turn(self, combat: Combat, actor: str) -> None:
        combat.end_turn()
        self._apply("combat_turn", {"actor": actor})

    def _tool_end_combat(self, args: dict[str, Any]) -> ToolResult:
        if self.world.combat is None:
            raise ValueError("no combat in progress")
        outcome = args.get("outcome")
        if outcome not in ("victory", "defeat", "fled"):
            raise ValueError(f"outcome must be victory|defeat|fled: {outcome!r}")
        xp_award = args.get("xp_award", 0)
        if not isinstance(xp_award, int) or isinstance(xp_award, bool) or xp_award < 0:
            raise ValueError(f"xp_award must be an int >= 0: {xp_award!r}")
        combat = self.world.combat
        pc_name = _pc_name(self.world)
        pc_record = combat.participants[pc_name]
        enemies = [c for name, c in combat.participants.items() if name != pc_name]
        pc_losing = pc_record.hp == 0 and not pc_record.death_successes >= 3
        all_enemies_down = all(enemy.hp == 0 for enemy in enemies)
        derived = "defeat" if pc_losing else ("victory" if all_enemies_down else None)
        if outcome != derived and not (derived is None and outcome == "fled"):
            raise ValueError(
                f"the engine derives {derived!r} from the committed state, not {outcome!r}"
            )
        lines = [f"Combat ends: {outcome}."]
        if outcome == "defeat":
            self._apply("pc_set_hp", {"hp": 1})
            lines.append("You wake at 1 HP, out of it.")
        elif pc_record.hp > 0:
            self._apply("pc_set_hp", {"hp": pc_record.hp})
            lines.append(f"{pc_name} is at {pc_record.hp} HP.")
        if outcome == "victory" and xp_award:
            self._apply("pc_gain_xp", {"amount": xp_award})
            sheet = self.world.pc.sheet
            lines.append(f"You gain {xp_award} XP (total {sheet.xp}, level {sheet.level}).")
        self._apply("combat_end", {"outcome": outcome})
        return ToolResult(
            name="end_combat", ok=True, message=lines[0], authoritative=lines
        )
