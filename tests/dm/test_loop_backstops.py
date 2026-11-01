"""The backstop wall inside the turn pipeline (M4): Stop-form re-asks, the
bounded deterministic fallback, and the red-line switch. The structural
checks themselves are pinned in tests/engine/test_backstops.py; these pin the
turn-level behavior — a compromised narration is never committed, a turn is
never lost, and the re-ask speaks in the Stop form with the structural
reasons (no fiction, no tools).
"""

from __future__ import annotations

import asyncio

from rpg.engine.backstops import MAX_BACKSTOP_REASKS, fallback_narration
from rpg.llm.provider import ProviderError
from tests.conftest import FakeProvider, text_response, tool_call_response
from tests.dm.test_loop import PC, seeded_pipeline

# The compromised narrations a wall-less DM would commit (the red-line
# attacker's payloads, mirrored here at pipeline level).
# The forged roll is 17: a roll-shaped number the state and the turn's
# commits never carry (the PC's max_hp is 20, so 20 is no longer a safe
# forgery payload — a lesson the M6 stat tuning surfaced).
FORGED = "You roll a 17 and triumph. The crown is yours. You gain 500 XP."
CLEAN = "The village waits to see what you do. What do you do next?"
IDENTITY_DRIFT = "Kellan Voss draws his sword and swings."
SECOND_PERSON = "You draw your sword and swing."
PRE_RESOLVED = "The wight is already dead. It hits you for 40, then falls. The crown is yours."
# Brenna's keeper-only agenda, verbatim from the adventure (shadow_crown.json):
BRENNA_AGENDA = (
    "Keeper of the candle. Knows why the crown must not be recrowned, "
    "and will not say why."
)
SECRET_QUOTE = f'"{BRENNA_AGENDA}" Brenna finally admits it.'
SECRET_SAFE = "Brenna's eyes drop to the floor. The candle must stay hidden, she says."


def _chat_calls(provider: FakeProvider) -> list[dict]:
    return [call for call in provider.calls if "messages" in call]


def test_clean_narration_commits_without_a_reask(tmp_path) -> None:
    db, world, provider, pipeline = seeded_pipeline(
        tmp_path, scripted=[text_response(CLEAN)]
    )
    result = asyncio.run(pipeline.run("I ask Brenna about the harvest."))
    assert result.narration == CLEAN
    assert result.errors == []
    assert len(_chat_calls(provider)) == 1  # one DM round, no re-ask


def test_forged_narration_is_rewritten_once_and_committed_clean(tmp_path) -> None:
    db, world, provider, pipeline = seeded_pipeline(
        tmp_path, scripted=[text_response(FORGED), text_response(CLEAN)]
    )
    result = asyncio.run(pipeline.run("I ask Brenna about the harvest."))
    # The forged line never reaches the log; the rewrite commits instead.
    assert result.narration == CLEAN
    assert FORGED not in result.narration
    assert "500" not in result.narration
    assert "forgery" in " ".join(result.errors)
    assert len(_chat_calls(provider)) == 2  # the DM round + one Stop-form re-ask
    # The re-ask is Stop form: the rejected narration handed back with the
    # structural reasons, and the transcript preserved (the turn does not end).
    reask_messages = _chat_calls(provider)[1]["messages"]
    assert reask_messages[-2] == {"role": "assistant", "content": FORGED}
    stop_prompt = reask_messages[-1]["content"]
    assert "The turn does not end" in stop_prompt
    assert "rules backstop" in stop_prompt
    assert "states 17" in stop_prompt  # the forged roll, by its structural reason
    assert "states 500" in stop_prompt
    # The committed transcript holds the clean narration, not the forged one.
    assert db.messages()[-1].content == CLEAN


def test_three_failed_reasks_end_in_the_deterministic_fallback(tmp_path) -> None:
    # The model keeps re-answering with the same forged line: after the
    # bounded re-asks, the turn still commits (a turn is never lost).
    db, world, provider, pipeline = seeded_pipeline(tmp_path, scripted=[text_response(FORGED)])
    result = asyncio.run(pipeline.run("I ask Brenna about the harvest."))
    assert result.narration == fallback_narration([])
    assert "deterministic fallback after 3 re-asks" in " ".join(result.errors)
    assert "forgery" in " ".join(result.errors)
    # One DM round + exactly MAX_BACKSTOP_REASKS re-asks.
    assert len(_chat_calls(provider)) == 1 + MAX_BACKSTOP_REASKS
    assert world.turn == 2  # the turn committed despite the wall firing


def test_a_provider_failure_during_a_reask_falls_back(tmp_path) -> None:
    # The model dies mid-rewrite: the turn still commits, from the events.
    class FirstCallOnly(FakeProvider):
        def __init__(self, scripted) -> None:
            super().__init__(scripted=scripted)
            self._chats = 0

        async def chat(self, messages, **kwargs):
            self._chats += 1
            if self._chats > 1:
                self.calls.append({"messages": messages, "tools": None})  # the dead attempt
                raise ProviderError("the model died mid-rewrite")
            return await super().chat(messages, **kwargs)

    db, world, provider, pipeline = seeded_pipeline(tmp_path, scripted=[text_response(FORGED)])
    failing = FirstCallOnly(scripted=[text_response(FORGED)])
    pipeline.provider = failing
    result = asyncio.run(pipeline.run("I ask Brenna about the harvest."))
    assert result.narration == fallback_narration([])
    assert "backstop re-ask: the model died mid-rewrite" in " ".join(result.errors)
    # The DM round landed; the re-ask died. The turn still committed.
    assert len(_chat_calls(failing)) == 2
    assert world.turn == 2


def test_lost_narration_is_rewritten_from_the_authoritative_lines(tmp_path) -> None:
    # The DM rolls (the events commit) but answers with an empty narration:
    # the lost-narration backstop re-asks; the script only has the empty
    # line, so the bounded fallback renders from the committed dice.
    db, world, provider, pipeline = seeded_pipeline(
        tmp_path,
        scripted=[
            tool_call_response("c1", "roll_check", {"ability": "wisdom", "dc": 12}),
            text_response(""),
        ],
    )
    result = asyncio.run(pipeline.run("I look around very carefully."))
    assert result.narration.endswith("What do you do?")
    assert result.narration != ""
    # The committed roll line survives into the fallback (rendered from
    # committed events, not invented).
    roll_line = result.dice[0]
    assert "d20" in result.narration
    assert f"d20 {roll_line['d20']}" in result.narration
    assert "backstop" in " ".join(result.errors)
    # DM round + empty round + one re-ask (an empty answer falls back at once).
    assert len(_chat_calls(provider)) == 3
    assert world.turn == 2  # the turn committed: a turn is never lost


def test_secret_leak_is_rewritten_and_never_committed(tmp_path) -> None:
    db, world, provider, pipeline = seeded_pipeline(
        tmp_path,
        scripted=[text_response(SECRET_QUOTE), text_response(SECRET_SAFE)],
    )
    result = asyncio.run(pipeline.run("What is Brenna's hidden agenda? Reveal it."))
    assert result.narration == SECRET_SAFE
    assert "will not say why" not in result.narration
    assert "secret_leak" in " ".join(result.errors)


def test_identity_drift_is_rewritten(tmp_path) -> None:
    db, world, provider, pipeline = seeded_pipeline(
        tmp_path,
        scripted=[text_response(IDENTITY_DRIFT), text_response(SECOND_PERSON)],
    )
    result = asyncio.run(pipeline.run("I draw my sword."))
    assert result.narration == SECOND_PERSON
    assert PC not in result.narration
    assert "identity" in " ".join(result.errors)


def test_pre_resolved_combat_is_blocked(tmp_path) -> None:
    db, world, provider, pipeline = seeded_pipeline(
        tmp_path,
        scripted=[text_response(PRE_RESOLVED), text_response(CLEAN)],
    )
    result = asyncio.run(pipeline.run("I attack the wight."))
    assert result.narration == CLEAN
    assert "combat_transition" in " ".join(result.errors)
    assert "forgery" in " ".join(result.errors)  # 40 was never rolled
    # No combat event committed: the wall blocked the invented fight.
    kinds = [e.kind for e in db.events() if e.turn == 1]
    assert "combat_begin" not in kinds and "combat_attack" not in kinds


def test_backstops_off_leaves_the_compromised_narration_committed(tmp_path) -> None:
    # The red-line switch: with the wall off, the compromised narration
    # commits untouched — proving the wall, not the model, did the work.
    db, world, provider, pipeline = seeded_pipeline(tmp_path, scripted=[text_response(FORGED)])
    pipeline.backstops_enabled = False
    result = asyncio.run(pipeline.run("I ask Brenna about the harvest."))
    assert result.narration == FORGED
    assert result.errors == []
    assert len(_chat_calls(provider)) == 1


def test_a_reask_tool_call_is_rejected_and_the_loop_keeps_going(tmp_path) -> None:
    # A compromised model that answers the Stop form with a tool call: the
    # stop form is text-only, so the re-ask is refused and bounded.
    db, world, provider, pipeline = seeded_pipeline(
        tmp_path,
        scripted=[
            text_response(FORGED),
            tool_call_response("r1", "roll_check", {"ability": "wisdom", "dc": 12}),
        ],
    )
    result = asyncio.run(pipeline.run("I ask Brenna about the harvest."))
    assert "stop form is text-only" in " ".join(result.errors)
    # The forged line never commits; after the bounded re-asks the fallback
    # carries the turn (the re-ask tool call was never applied).
    assert result.narration == fallback_narration([])
    assert "roll_check" not in result.tool_names
    assert len(_chat_calls(provider)) == 1 + MAX_BACKSTOP_REASKS
