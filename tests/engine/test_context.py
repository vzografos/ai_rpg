"""Context builder tests (M2): stable head, volatile tail, trimming, metering.

Pins the state/memory layout layout — including the keeper-only guarantees (NPC
agendas never enter the volatile tail) and the bounded-meter behavior.
"""

from __future__ import annotations

from rpg.engine.context import (
    DEFAULT_CONTEXT_BUDGET,
    DEFAULT_MAX_MESSAGES,
    build_context,
    dump_context,
    estimate_tokens,
    stable_head,
    state_block,
)
from rpg.engine.events import Message
from rpg.engine.state import WorldState
from rpg.llm import prompts

SEED = {
    "pc": {
        "name": "Mira",
        "hp": 9,
        "max_hp": 14,
        "ac": 14,
        "abilities": {
            "strength": 14,
            "dexterity": 10,
            "constitution": 12,
            "intelligence": 10,
            "wisdom": 10,
            "charisma": 10,
        },
        "inventory": {"Torch": 2, "Ration": 3},
        "gold": 21,
        "xp": 900,
    },
    "pc_location": "The tavern",
    "locations": [
        {"name": "The village square", "description": "A quiet square.", "exits": ["The tavern"]},
        {
            "name": "The tavern",
            "description": "Woodsmoke and low voices.",
            "exits": ["The village square", "The cellar"],
        },
        {"name": "The cellar", "description": "Cold and damp.", "exits": []},
    ],
    "npcs": [
        {
            "name": "Brenna",
            "location": "The tavern",
            "description": "The tavern keeper.",
            "agenda": "She is hunting the shadow crown and will not say so.",
        },
        {
            "name": "Old Fen",
            "location": "The village square",
            "description": "A one-eared cartographer.",
            "status": "dead",
        },
    ],
    "quests": [
        {
            "name": "Find the shadow crown",
            "description": "The mayor pays for it.",
            "status": "active",
            "reward": "200 gp",
        },
        {"name": "Guard the bridge", "description": "", "status": "open"},
    ],
    "items": [
        {"name": "Rusty key", "location": "The tavern", "description": "Smells of iron."},
        {"name": "Silver dagger", "holder": "npc:Brenna"},
    ],
}


def make_world() -> WorldState:
    world = WorldState()
    world.apply_event("world_init", SEED, turn=0)
    world.apply_event(
        "fact_add",
        {
            "subject": "Brenna",
            "predicate": "fears",
            "value": "the crown",
            "source": "her hands shook",
        },
        turn=1,
    )
    world.apply_event(
        "combat_begin",
        {
            "participants": [
                {
                    "name": "Mira",
                    "ac": 14,
                    "hp": 9,
                    "max_hp": 14,
                    "dexterity": 10,
                    "attack_bonus": 3,
                    "damage": "1d8+1",
                },
                {
                    "name": "Grak",
                    "ac": 12,
                    "hp": 0,
                    "max_hp": 8,
                    "dexterity": 12,
                    "attack_bonus": 3,
                    "damage": "1d6+2",
                    "conditions": ["unconscious"],
                },
            ],
            "order": [{"name": "Mira", "initiative": 17}, {"name": "Grak", "initiative": 9}],
        },
        turn=2,
    )
    return world


def transcript(n: int) -> list[Message]:
    messages = []
    for i in range(n):
        messages.append(Message(turn=i, role="user", content=f"I do the thing number {i}."))
        messages.append(
            Message(turn=i, role="narration", content=f"The world answers with detail number {i}. ")
        )
    return messages


# --- metering -----------------------------------------------------------------


def test_estimate_tokens_is_the_char_heuristic():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2
    assert estimate_tokens("x" * 400) == 100


# --- stable head ------------------------------------------------------------------


def test_stable_head_carries_the_dm_contract():
    head = stable_head(make_world())
    assert prompts.DM_IDENTITY in head
    assert prompts.RULES_SUMMARY in head
    assert prompts.GM_STANDARDS in head
    assert prompts.KEEPER_DISCIPLINE in head
    assert "second person" in head


def test_stable_head_appends_spine_and_summary_when_present():
    world = make_world()
    assert "[NOW]" not in stable_head(world)
    head = stable_head(world, spine_now="Beat 2: the cellar door")
    assert "[NOW]" in head and "the cellar door" in head
    world.summary = "The crown was last seen in the crypt."
    head = stable_head(world)
    assert "Campaign summary:" in head and "the crypt" in head


# --- volatile tail --------------------------------------------------------------------


def test_state_block_opens_with_the_engine_header():
    block = state_block(make_world())
    assert block.startswith(prompts.STATE_HEADER)


def test_state_block_shows_the_playable_world():
    block = state_block(make_world())
    assert "Location: The tavern" in block
    assert "Woodsmoke" in block
    assert "Exits: The village square, The cellar" in block
    assert "PC: Mira" in block
    assert "level 3" in block  # 900 XP -> level 3 (derived)
    assert "HP 9/14" in block
    assert "gold 21" in block
    assert "2x Torch" in block and "3x Ration" in block
    assert "NPC: Brenna (in The tavern)" in block
    assert "NPC: Old Fen" in block and "— dead" in block
    assert "Quest [active]: Find the shadow crown" in block
    assert "objective unclear" in block  # the open quest with no description
    assert "Item: Rusty key (at The tavern)" in block
    assert "Item: Silver dagger (with npc:Brenna)" in block
    assert "Fact (turn 1): Brenna fears the crown" in block
    assert "Combat (round 0)" in block
    assert "Grak HP 0/8 [unconscious]" in block
    assert "Acting now: Mira" in block


def test_state_block_never_leaks_keeper_only_data():
    block = state_block(make_world())
    dump = dump_context(make_world())
    for text in (block, dump):
        assert "hunting the shadow crown" not in text  # the agenda stays behind the curtain
        assert "keeper-only" not in text


def test_empty_world_stays_readable():
    block = state_block(WorldState())
    assert block == prompts.STATE_HEADER


# --- assembly + trimming ---------------------------------------------------------------


def test_context_assembles_system_and_user_messages():
    context = build_context(
        make_world(), messages=transcript(3), player_line="I open the cellar door."
    )
    messages = context.messages()
    assert [m["role"] for m in messages] == ["system", "user"]
    assert messages[0]["content"] == context.system
    tail = messages[1]["content"]
    assert tail.startswith(prompts.STATE_HEADER)
    assert "Recent transcript:" in tail
    assert "You: I do the thing number 2." in tail
    assert "DM: The world answers with detail number 2." in tail
    assert tail.rstrip().endswith("You: I open the cellar door.")
    assert context.meter.fits()


def test_transcript_keeps_only_the_recent_messages():
    context = build_context(make_world(), messages=transcript(20))
    # 20 turns x 2 lines = 40 messages; the tail keeps the last 12 (turns 14-19).
    assert len(context.transcript) == DEFAULT_MAX_MESSAGES
    assert context.transcript[0].content.startswith("I do the thing number 14")
    assert context.transcript[-1].content.startswith("The world answers with detail number 19")


def test_transcript_trims_oldest_until_the_budget_fits():
    world = make_world()
    long_history = transcript(30)
    tail = long_history[-DEFAULT_MAX_MESSAGES:]
    tight_budget = (
        estimate_tokens(stable_head(world))
        + estimate_tokens(state_block(world))
        + sum(estimate_tokens(m.content) for m in tail[-2:])
        + 1
    )
    # The third-oldest message of the tail alone costs more than the +1 slack,
    # so the only way to fit is to trim down to the two-line floor.
    assert estimate_tokens(tail[-3].content) > 1
    context = build_context(world, messages=long_history, budget=tight_budget)
    assert context.meter.fits()
    assert len(context.transcript) == 2  # the floor: the last two lines
    assert context.transcript == tail[-2:]


def test_over_budget_is_reported_when_there_is_nothing_left_to_trim():
    world = make_world()
    context = build_context(world, messages=transcript(5), budget=1)
    assert not context.meter.fits()
    assert len(context.transcript) == 2
    assert context.meter.total > 1


def test_meter_breakdown_covers_every_section():
    world = make_world()
    context = build_context(world, messages=transcript(2), player_line="hi")
    meter = context.meter
    assert meter.stable_head == estimate_tokens(context.system)
    assert meter.state_block == estimate_tokens(context.state_block)
    assert meter.transcript == sum(estimate_tokens(m.content) for m in context.transcript)
    assert meter.player_line == estimate_tokens("hi")
    assert (
        meter.total == meter.stable_head + meter.state_block + meter.transcript + meter.player_line
    )
    assert meter.budget == DEFAULT_CONTEXT_BUDGET


# --- the dump ------------------------------------------------------------------------------


def test_dump_context_is_readable_and_metered():
    world = make_world()
    dump = dump_context(world, messages=transcript(4), player_line="I open the cellar door.")
    # make_world applies its events without a commit, so the turn counter is still 0.
    assert "context dump (turn 0)" in dump
    assert "stable head" in dump
    assert "state block" in dump
    assert "transcript" in dump
    assert "est-tokens" in dump
    assert "fits" in dump
    assert "--- stable head (system message) ---" in dump
    assert "--- volatile tail (user message) ---" in dump
    assert prompts.DM_IDENTITY in dump
    assert "You: I open the cellar door." in dump


def test_dump_context_reports_over_budget():
    world = make_world()
    dump = dump_context(world, messages=transcript(30), budget=1)
    assert "OVER BUDGET" in dump
