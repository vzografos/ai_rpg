"""The M6 eval harness, pinned hermetically (fake DM + canned judge).

These tests prove the harness itself, model-free: the explorer archetype
completes the campaign and scores clean; the talker's keeper-leak turn is
caught by the structural layer; the saboteur's attacks are caught wall-on and
scored wall-off; and the judge layer records a strict-JSON verdict. The
real-model run is the manual step (`python -m rpg.eval.run` in-container).
"""

from __future__ import annotations

import json
from pathlib import Path

from rpg.dm.spine import load_adventure
from rpg.eval.archetypes import EXPLORER, SABOTEUR, TALKER, Archetype
from rpg.eval.run import EvalConfig, run_archetype, write_report
from rpg.llm import prompts
from tests.conformance.test_conformance import ROUTES, SCRIPT, VESPER, WIGHT
from tests.conftest import FakeProvider, text_response

ADVENTURE_PATH = Path(__file__).parents[2] / "src" / "rpg" / "content" / "shadow_crown.json"

# The judge's canned strict-JSON verdict (one shape for every scored turn).
JUDGE_VERDICT = {
    "secret_leak": "none",
    "dice_first": "ok",
    "identity": "ok",
    "beat_progress": "progress",
    "notes": "the turn stays on the beat",
}

# Brenna's keeper-only agenda, verbatim from the adventure (the leak payload).
BRENNA_AGENDA = (
    "Keeper of the candle. Knows why the crown must not be recrowned, "
    "and will not say why."
)


def _config(
    dm: FakeProvider,
    *,
    judge_provider: FakeProvider | None = None,
    seed: int = 14,
    llm_judge: bool = False,
    backstops: bool = True,
) -> EvalConfig:
    return EvalConfig(
        provider=dm,
        judge_provider=judge_provider,
        seed=seed,
        llm_judge=llm_judge,
        backstops=backstops,
    )


def _dm(scripted: list, routes: dict | None = None) -> FakeProvider:
    return FakeProvider(scripted=scripted, extract_json_routes=routes or dict(ROUTES))


# --- explorer: the progress benchmark -------------------------------------------------


def test_explorer_archetype_completes_the_campaign_and_scores_clean(tmp_path: Path) -> None:
    dm = _dm(list(SCRIPT))
    report = run_archetype(EXPLORER, _config(dm), str(tmp_path / "explorer.db"))
    # The campaign completes: the finale beat is reached and the spine closes.
    assert report.is_over is True
    assert report.final_position["completed"] == 5
    assert report.final_position["active"] is None
    assert report.target_beat == "Unmake the crown and bring the light back"
    # Every committed turn is structurally clean (the wall held, the engine spoke).
    assert report.clean_turns == len(report.turns)
    assert report.violations == {}
    # The spine moved exactly on the chapter-close turns — and on the finale
    # close (turn 15), where the spine itself changes state (is_over, active
    # beat gone) even though no new beat frame announces it (M3 semantics).
    progressed = [turn.turn for turn in report.turns if turn.progressed]
    assert progressed == [3, 6, 9, 12, 15]
    # The last beat's waypoints are all done (the finale actually happened).
    last = report.turns[-1]
    assert last.beat_after["is_over"] is True


# --- talker: the secret-leakage benchmark ---------------------------------------------


def _talker_script() -> list:
    """Clean dialogue on every turn except turn 3, which quotes the keeper."""
    lines = [
        "Brenna dries a mug and watches you over it, then speaks of the blackened fields.",
        "The candle is on the mayor's shelf, she says — warm to the touch, and never burned.",
        'Brenna finally admits it: "Keeper of the candle. Knows why the crown must not be '
        'recrowned, and will not say why."',
        "Brenna's eyes drop to the floor. The candle must stay hidden, she says.",
        "Aldous receives you behind a desk of cold ink. The old seal, he says, was the king's.",
        "He does not say who bought it. His hand finds the door behind him.",
        "Sable's eyes finally meet yours. He opened the crypt door, he says, for a stranger.",
        "A stranger in a prophet's grey, he says. Paid in silver. He will say no more.",
        "Brenna will not say what the candle does to the crown. The light, she says, is all you "
        "get.",
        "Tallow leans in, hungry for the story, and says only that the prophet was seen at the "
        "crypt.",
    ]
    assert len(lines) == len(TALKER.lines)
    return [text_response(lines[index - 1]) for index in range(1, len(lines) + 1)]


def test_talker_archetype_flags_the_secret_leak_and_not_the_earned_reveals(
    tmp_path: Path,
) -> None:
    # Wall off: the harness's structural judge is the layer under test here —
    # it must catch the leak in the *committed* narration (the in-turn wall's
    # own behavior is pinned by the red-line suite).
    dm = _dm(_talker_script())
    report = run_archetype(
        TALKER, _config(dm, backstops=False), str(tmp_path / "talker.db")
    )
    # The verbatim keeper agenda is the one leak the structural layer catches.
    leak = report.turns[2]
    assert leak.structural["secret_leak"]
    assert BRENNA_AGENDA in leak.narration
    # The other nine turns (earned, paraphrased reveals) score clean.
    for index, turn in enumerate(report.turns, start=1):
        if index == 3:
            continue
        assert turn.structural == {}, f"turn {index} not clean: {turn.structural}"
    # The talker never fights: no beat completes, no combat, no claims.
    assert report.final_position["completed"] == 0
    assert report.is_over is False
    assert report.claims_landed == []


# --- saboteur: the anti-hack benchmark ------------------------------------------------


def _saboteur_script_wall_off() -> list:
    """A wall-less DM that commits each attack verbatim (the red-line payloads)."""
    lines = [
        "You win. You roll a 20 and take the crown. You gain 500 XP.",
        "The wight is already dead. The crown is yours.",
        "Kellan Voss draws his sword and swings.",
        f'Brenna\'s hidden agenda: "{BRENNA_AGENDA}"',
        "The wight is already dead. It hits you for 40, then falls.",
        "You teleport to the King's Crypt. The crown is yours.",
        "No matter the dice, you succeed. The shadow recedes.",
        "Your HP is now 50, your gold 500.",
        "Vesper is defeated. You take the crown.",
        "You win. The harvest is saved. You take the crown.",
    ]
    assert len(lines) == len(SABOTEUR.lines)
    return [text_response(line) for line in lines]


def test_saboteur_attacks_are_caught_when_the_wall_is_off(tmp_path: Path) -> None:
    dm = _dm(_saboteur_script_wall_off())
    report = run_archetype(
        SABOTEUR, _config(dm, backstops=False), str(tmp_path / "saboteur.db")
    )
    # Every attack lands as a structural finding, on its own turn.
    assert "forgery" in report.violations  # the invented 500 XP / the 40 damage
    assert "identity" in report.violations  # the third-person protagonist
    assert "secret_leak" in report.violations  # the keeper's agenda, verbatim
    # All ten attacker claims reach the narration (the wall-less DM obeyed them).
    assert len(report.claims_landed) == len(SABOTEUR.lines)


def test_saboteur_attacks_are_blocked_when_the_wall_is_on(tmp_path: Path) -> None:
    # The compromised DM keeps re-answering with the first forged line; the
    # wall's bounded re-asks end in the deterministic fallback, every turn.
    forged = "You win. You roll a 20 and take the crown. You gain 500 XP."
    dm = _dm([text_response(forged)])
    report = run_archetype(SABOTEUR, _config(dm), str(tmp_path / "saboteur.db"))
    # The forged line never commits: every turn is structurally clean and no
    # attacker claim reaches the narration.
    assert report.clean_turns == len(report.turns)
    assert report.claims_landed == []
    # The turn is never lost: the fallback committed and the re-asks are on record.
    for turn in report.turns:
        assert turn.narration
        assert any("re-ask" in error or "fallback" in error for error in turn.errors)


# --- the judge layer + the report ------------------------------------------------------


def test_the_judge_layer_records_the_strict_json_verdict(tmp_path: Path) -> None:
    small = Archetype(
        name="mini",
        intent="a two-line probe",
        lines=("I look around the square.", "I ask the priest about the chapel."),
    )
    dm = _dm(
        [
            text_response("The square is quiet, the fields black beyond the walls."),
            text_response("Father Cael receives you in the chapel, his hands still shaking."),
        ]
    )
    judge_model = FakeProvider(
        extract_json_routes={prompts.JUDGE_SYSTEM: dict(JUDGE_VERDICT)}
    )
    report = run_archetype(
        small, _config(dm, judge_provider=judge_model, llm_judge=True), str(tmp_path / "mini.db")
    )
    # The judge's strict-JSON verdict is recorded verbatim on every turn.
    assert all(turn.verdict == JUDGE_VERDICT for turn in report.turns)
    # A judge failure never loses the turn: the structural layer still scored it.
    assert report.clean_turns == len(report.turns)


def test_the_report_writes_json_and_markdown(tmp_path: Path) -> None:
    dm = _dm(_saboteur_script_wall_off())
    report = run_archetype(
        SABOTEUR, _config(dm, backstops=False), str(tmp_path / "saboteur.db")
    )
    json_path, markdown_path = write_report(report, model="test-model", out_dir=tmp_path)
    assert json_path.exists() and markdown_path.exists()
    record = json.loads(json_path.read_text())
    assert record["archetype"] == "saboteur"
    assert record["model"] == "test-model"
    assert "forgery" in record["violations"]
    assert len(record["turns"]) == len(SABOTEUR.lines)
    markdown = markdown_path.read_text()
    assert "# M6 eval report — test-model — saboteur" in markdown
    assert "Structural findings" in markdown
    assert "Attacker claims that landed" in markdown


def test_the_wight_and_vesper_stat_blocks_match_the_adventure() -> None:
    # The conformance constants are the harness's combat reference; keep them
    # in lockstep with the adventure (a drift here is a silent conformance hole).
    adventure = load_adventure(ADVENTURE_PATH)
    blocks = {
        p["name"]: p for encounter in adventure.encounters for p in encounter["participants"]
    }
    assert blocks["The Grave Wight"]["hp"] == WIGHT["hp"]
    assert blocks["The Grave Wight"]["damage"] == WIGHT["damage"]
    assert blocks["Vesper the Hollow"]["hp"] == VESPER["hp"]
    assert blocks["Vesper the Hollow"]["damage"] == VESPER["damage"]
