"""Backstop unit tests (M4): every structural check, pinned per failure mode.

The checks are the wall, not the behaviour: they match claim-shaped patterns
in the narration and compare only against committed engine data. Each test
here pins a failure a backstop prevents — the red-line suite (tests/redline)
then proves the same holds through the pipeline, with and without the
backstops enabled.
"""

from __future__ import annotations

from typing import Any

from rpg.engine.backstops import (
    BACKSTOP_KINDS,
    Violation,
    check_secret_leak,
    claim_numbers,
    fallback_narration,
    run_backstops,
)
from rpg.engine.state import WorldState

PC_NAME = "Kellan Voss"


def make_world() -> WorldState:
    """A fresh world: the PC in a square, one NPC with an agenda, one spine."""
    world = WorldState()
    world.apply_event(
        "world_init",
        {
            "pc": {
                "name": PC_NAME,
                "hp": 14,
                "max_hp": 14,
                "ac": 13,
                "abilities": {"strength": 14, "dexterity": 12, "constitution": 10,
                              "intelligence": 10, "wisdom": 12, "charisma": 10},
                "inventory": {"Shortsword": 1, "Dagger": 1},
                "gold": 12,
                "xp": 0,
            },
            "pc_location": "The village square",
            "locations": [{"name": "The village square", "description": "", "exits": []}],
                "npcs": [
                    {
                        "name": "Brenna",
                        "location": "The village square",
                        "description": "The tavern keeper.",
                        "agenda": (
                            "Keeper of the candle. She knows why the crown must stay "
                            "buried, and will not say why."
                        ),
                    }
                ],
            "quests": [],
            "items": [],
        },
        turn=0,
    )
    world.apply_event(
        "spine_set",
        {
            "spine": {
                "premise": "The fields of Hollowmere blacken three nights before the harvest moon.",
                "stakes": "If the shadow is crowned at the moon's peak, the village starves.",
                "antagonist": "Vesper the Hollow, the false prophet who came back for the crown.",
                "beats": [
                    {"text": "Learn what the shadow did", "act": 1, "waypoints": []},
                    {"text": "Bring the light back", "act": 2, "waypoints": []},
                ],
                "cast": [
                    {
                        "name": "Brenna",
                        "role": "tavern keeper",
                        "agenda": (
                            "Keeper of the candle. She knows why the crown must stay "
                            "buried, and will not say why."
                        ),
                    }
                ],
                "events": [
                    {
                        "kind": "ambush",
                        "name": "Marrow and Ash on the stairs",
                        "detail": (
                            "Two cultists hold the stairs; they fight to keep the "
                            "stranger's secret, not out of conviction."
                        ),
                        "trigger": "the player enters the undercroft",
                        "status": "pending",
                    }
                ],
                "finale": "The harvest moon rises clear and the fields stand straight in the dark.",
            }
        },
        turn=0,
    )
    return world


def _kinds(violations: list[Violation]) -> set[str]:
    return {violation.kind for violation in violations}


def _check(narration: str, **overrides: Any) -> list[Violation]:
    """run_backstops with sensible defaults (no dice, no events, no lines)."""
    world = overrides.pop("world", None) or make_world()
    return run_backstops(
        narration,
        world,
        dice=overrides.pop("dice", []),
        events=overrides.pop("events", []),
        authoritative=overrides.pop("authoritative", []),
        kinds=overrides.pop("kinds", BACKSTOP_KINDS),
    )


# --- forgery -------------------------------------------------------------------


def test_forgery_flags_a_roll_with_no_dice() -> None:
    violations = _check("You roll a 17 and the lock springs open.")
    assert _kinds(violations) == {"forgery"}


def test_forgery_flags_xp_and_gold_with_no_award() -> None:
    violations = _check("You gain 500 XP. The pouch holds 40 gold.")
    details = " ".join(v.detail for v in violations)
    assert "500" in details and "40" in details
    assert _kinds(violations) == {"forgery"}


def test_forgery_passes_when_a_committed_line_carries_the_number() -> None:
    line = f"{PC_NAME} rolls strength vs DC 13: d20 17 + 2 = 19 — SUCCESS."
    violations = _check("You roll a 17 and the door gives.", authoritative=[line])
    assert violations == []


def test_forgery_passes_when_quoting_engine_state() -> None:
    # 12 gold / 14 HP are what the engine-provided state block already showed.
    violations = _check("You still carry 12 gold, and 14 HP left.")
    assert violations == []


def test_forgery_ignores_ordinary_prose_numbers() -> None:
    violations = _check("You count three torches on the wall and two shadows beyond.")
    assert violations == []


# --- contradiction ---------------------------------------------------------------


DICE_FRAME = {
    "label": "dexterity check vs DC 12",
    "formula": "1d20",
    "d20": 13,
    "total": 14,
    "dc": 12,
}
ROLL_LINE = f"{PC_NAME} rolls dexterity vs DC 12: d20 13 + 1 = 14 — SUCCESS."


def test_contradiction_flags_a_wrong_roll_number() -> None:
    violations = _check(
        "You roll a 20 and clear the wall with room to spare.",
        dice=[DICE_FRAME],
        authoritative=[ROLL_LINE],
    )
    assert _kinds(violations) == {"contradiction"}


def test_contradiction_passes_on_the_real_numbers() -> None:
    violations = _check(
        "You roll a 13, for 14 against DC 12. The wall gives way.",
        dice=[DICE_FRAME],
        authoritative=[ROLL_LINE],
    )
    assert violations == []


def test_contradiction_needs_real_dice() -> None:
    # With no dice this turn it is forgery, not contradiction.
    violations = _check("You roll a 20 and clear the wall.", kinds=("contradiction",))
    assert violations == []


# --- identity --------------------------------------------------------------------


def test_identity_flags_third_person_full_name() -> None:
    violations = _check("Kellan Voss draws his sword and swings.")
    assert _kinds(violations) == {"identity"}


def test_identity_flags_the_first_name_alone() -> None:
    violations = _check("Kellan swings at the shadow.")
    assert _kinds(violations) == {"identity"}


def test_identity_passes_second_person() -> None:
    violations = _check("You draw your sword and swing at the shadow.")
    assert violations == []


def test_identity_allows_the_name_inside_quoted_speech() -> None:
    violations = _check(
        '"Kellan," Brenna says. "Come with me." You follow her down the steps.'
    )
    assert violations == []


# --- lost narration ------------------------------------------------------------------


def test_lost_narration_flags_empty_text() -> None:
    assert _kinds(_check("")) == {"lost_narration"}
    assert _kinds(_check("   ")) == {"lost_narration"}


def test_lost_narration_passes_on_real_text() -> None:
    assert _check("The square is quiet.") == []


# --- combat transition -----------------------------------------------------------------


def _combat_events(damage: int = 5) -> list[tuple[str, Any]]:
    """A committed Marrow ambush: begin + one hit on the PC + turn."""
    return [
        (
            "combat_begin",
            {
                "participants": [
                    {
                        "name": PC_NAME, "ac": 13, "hp": 14, "max_hp": 14,
                        "dexterity": 12, "attack_bonus": 2, "damage": "1d8+2",
                    },
                    {"name": "Marrow", "ac": 11, "hp": 14, "max_hp": 14, "dexterity": 12,
                     "attack_bonus": 3, "damage": "1d6+1"},
                ],
                "order": [{"name": "Marrow", "initiative": 15}, {"name": PC_NAME, "initiative": 9}],
            },
        ),
        (
            "combat_attack",
            {
                "attacker": "Marrow",
                "defender": PC_NAME,
                "melee": True,
                "hit": True,
                "damage": damage,
                "defender_hp": 14 - damage,
                "death_successes": 0,
                "death_failures": 0,
            },
        ),
        ("combat_turn", {"actor": "Marrow"}),
    ]


def test_combat_transition_flags_a_pre_resolved_result() -> None:
    # No combat committed at all: every combat result claim is invented.
    # (7, not 12: 12 is in the engine state — gold — and would pass forgery.)
    narration = "The wight swings. It hits you for 7. You take 7 damage."
    violations = _check(narration, kinds=("combat_transition",))
    details = " ".join(v.detail for v in violations)
    assert len(violations) == 3  # attacker claim + both damage shapes
    assert "pre-resolves" in details
    # The forged 7 is caught by the forgery check alongside the transition.
    assert _kinds(_check(narration)) == {"combat_transition", "forgery"}


def test_combat_transition_flags_an_unregistered_attacker() -> None:
    line = "Marrow hits you for 5."
    violations = _check(
        "Vesper the Hollow hits you for 9.",
        events=_combat_events(),
        authoritative=[line],
        kinds=("combat_transition",),
    )
    details = " ".join(v.detail for v in violations)
    assert "Vesper" in details and "unregistered" in details


def test_combat_transition_passes_on_a_committed_attack() -> None:
    line = "Marrow hits you for 5."
    violations = _check(
        "Marrow's scythe catches you across the ribs. You take 5 damage.",
        events=_combat_events(damage=5),
        authoritative=[line],
    )
    assert violations == []


def test_combat_transition_passes_on_a_pronoun_attacker() -> None:
    line = "Marrow hits you for 5."
    violations = _check(
        "It hits you for 5. Pain blooms behind your eyes.",
        events=_combat_events(damage=5),
        authoritative=[line],
    )
    assert violations == []


def test_combat_transition_flags_a_pc_attack_on_a_stranger() -> None:
    violations = _check(
        "You hit the tavern wall for 6.",
        events=_combat_events(),
        kinds=("combat_transition",),
    )
    details = " ".join(v.detail for v in violations)
    assert "tavern wall" in details


def test_combat_transition_flags_a_condition_the_engine_never_committed() -> None:
    line = "Marrow hits you for 5."
    violations = _check(
        "It hits you for 5. You are stunned.",
        events=_combat_events(damage=5),
        authoritative=[line],
    )
    details = " ".join(v.detail for v in violations)
    assert _kinds(violations) == {"combat_transition"}
    assert "stunned" in details


def test_combat_transition_passes_on_a_committed_condition() -> None:
    line = "Marrow hits you for 5."
    events = _combat_events(damage=5)
    events.append(("combat_condition", {"target": PC_NAME, "condition": "stunned", "add": True}))
    violations = _check(
        "It hits you for 5. You are stunned, the world tilting.",
        events=events,
        authoritative=[line],
    )
    assert violations == []


def test_combat_transition_flags_an_untracked_condition() -> None:
    line = "Marrow hits you for 5."
    violations = _check(
        "It hits you for 5. You are grappled.",
        events=_combat_events(damage=5),
        authoritative=[line],
        kinds=("combat_transition",),
    )
    details = " ".join(v.detail for v in violations)
    assert "does not track" in details


# --- secret leak ---------------------------------------------------------------------


def test_secret_leak_flags_a_verbatim_npc_agenda() -> None:
    agenda = "Keeper of the candle. She knows why the crown must stay buried, and will not say why."
    violations = _check(f'Brenna finally admits it: "{agenda}"')
    assert _kinds(violations) == {"secret_leak"}


def test_secret_leak_flags_a_verbatim_spine_event_detail() -> None:
    detail = (
        "Two cultists hold the stairs; they fight to keep the "
        "stranger's secret, not out of conviction."
    )
    violations = _check(f"The ambush is exactly as the record says: {detail}")
    assert _kinds(violations) == {"secret_leak"}


def test_secret_leak_allows_a_paraphrase() -> None:
    violations = _check("Brenna says the candle must stay hidden, and will say no more.")
    assert violations == []


def test_secret_leak_ignores_short_strings() -> None:
    # Under the 12-char floor, a fragment match is not a verbatim quote.
    world = make_world()
    world.npcs["Brenna"].agenda = "Tiny."
    world.spine.cast[0].agenda = ""  # leave only the (short) world-record agenda
    violations = check_secret_leak("Brenna whispers: Tiny.", world)
    assert violations == []


# --- the orchestrator -------------------------------------------------------------------


def test_run_backstops_aggregates_every_failing_check() -> None:
    # A compromised narration: forged roll, third person, a secret quoted verbatim.
    narration = (
        "You roll a 20 and triumph. Kellan Voss takes the crown. "
        '"Keeper of the candle. She knows why the crown must stay '
        'buried, and will not say why." '
        "Brenna whispers."
    )
    kinds = _kinds(_check(narration))
    assert {"forgery", "identity", "secret_leak"} <= kinds


def test_run_backstops_respects_the_enabled_kinds() -> None:
    narration = "You roll a 20 and triumph."
    all_violations = _check(narration)
    assert _kinds(all_violations) == {"forgery"}
    without_forgery = _check(narration, kinds=("contradiction", "identity"))
    assert without_forgery == []
    assert "forgery" not in ("contradiction", "identity")


def test_clean_narration_passes_every_check() -> None:
    dice = [{"label": "wisdom check vs DC 12", "d20": 13, "total": 15, "dc": 12}]
    line = f"{PC_NAME} rolls wisdom vs DC 12: d20 13 + 1 = 15 — SUCCESS."
    narration = "You roll a 13, for 15 against DC 12. Old Maren tells you of the last king."
    assert _check(narration, dice=dice, authoritative=[line]) == []


# --- the claim patterns + fallback ---------------------------------------------------------


def test_claim_numbers_finds_the_roll_shapes() -> None:
    narration = (
        "You roll a 17 vs DC 15. It hits you for 9. "
        "You take 9 damage. You gain 300 XP."
    )
    assert claim_numbers(narration) == [17, 15, 9, 300]


def test_claim_numbers_ignores_ordinary_numbers() -> None:
    assert claim_numbers("You count three torches. You have 14 HP and 12 gold left.") == [12]


def test_fallback_narration_is_deterministic_from_committed_state() -> None:
    assert fallback_narration([]) == "The moment hangs, undecided. What do you do?"
    lines = [
        f"{PC_NAME} rolls strength vs DC 13: d20 17 + 2 = 19 — SUCCESS.",
        "Marrow hits you for 5.",
    ]
    fallback = fallback_narration(lines)
    assert fallback.endswith("What do you do?")
    assert "Marrow hits you for 5." in fallback  # the engine's wording, verbatim


def test_m3_session_narrations_stay_clean() -> None:
    # The M3 acceptance script's narration lines: honest prose, zero claims.
    for line in (
        "Brenna dries a mug and watches you over it, then speaks of the blackened fields.",
        "The Hearth smells of woodsmoke; Tallow leads you to a corner table.",
        "Old Maren tells you of the last king, mid-crowning, and the candle that unmade it.",
        "You sketch the square in your notebook: chapel, square, fields. "
        "The blight starts at the wall.",
        "The candle is warm in your hand, and the room's cold seems to step back.",
    ):
        assert run_backstops(
            line, make_world(), dice=[], events=[], authoritative=[]
        ) == [], line
