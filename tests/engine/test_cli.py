"""CLI world-command tests (M2): new / turn / world-status / dump-context.

The acceptance demo runs as separate processes (subprocess, the same way
`docker exec` would invoke it): create a world, commit turns, "kill", and
re-read — the state must be identical.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from rpg.engine.events import WorldDB
from rpg.llm import prompts


def run_cli(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "rpg.cli", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_new_creates_a_committed_world(tmp_path: Path) -> None:
    db = tmp_path / "w.db"
    result = run_cli(["new", "Mira", "--hp", "14", "--db", str(db)])
    assert result.returncode == 0, result.stderr
    assert db.exists()
    with WorldDB(db) as wdb:
        world = wdb.load_world()
        assert wdb.last_snapshot() is not None
    assert world is not None
    assert world.pc.sheet.name == "Mira" and world.pc.sheet.hp == 14
    assert world.turn == 1
    # Re-running `new` on an existing world is rejected.
    result = run_cli(["new", "Other", "--db", str(db)])
    assert result.returncode == 1
    assert "already exists" in result.stderr


def test_turn_commits_messages_and_a_snapshot(tmp_path: Path) -> None:
    db = tmp_path / "w.db"
    assert run_cli(["new", "Mira", "--db", str(db)]).returncode == 0
    result = run_cli(
        ["turn", "I search the tavern.", "You find a loose floorboard.", "--db", str(db)]
    )
    assert result.returncode == 0, result.stderr
    result = run_cli(["turn", "I pry it up.", "Beneath: a rusted key.", "--db", str(db)])
    assert result.returncode == 0, result.stderr
    with WorldDB(db) as wdb:
        world = wdb.load_world()
        assert world is not None and world.turn == 3
        assert [(m.role, m.content) for m in wdb.messages()] == [
            ("user", "I search the tavern."),
            ("narration", "You find a loose floorboard."),
            ("user", "I pry it up."),
            ("narration", "Beneath: a rusted key."),
        ]
        assert wdb.last_snapshot() is not None and wdb.last_snapshot()[0] == 2


def test_world_status_and_dump_context_are_readable(tmp_path: Path) -> None:
    db = tmp_path / "w.db"
    assert run_cli(["new", "Mira", "--db", str(db)]).returncode == 0
    result = run_cli(["world-status", "--db", str(db)])
    assert result.returncode == 0, result.stderr
    body = json.loads(result.stdout)
    assert body["turn"] == 1
    assert body["events"] == 1
    assert body["world"]["pc"]["name"] == "Mira"
    assert body["last_snapshot"]["hash"]

    result = run_cli(["dump-context", "--db", str(db)])
    assert result.returncode == 0, result.stderr
    dump = result.stdout
    assert "context dump" in dump
    assert prompts.STATE_HEADER in dump
    assert "est-tokens" in dump
    assert "PC: Mira" in dump


def test_commands_require_a_world_first(tmp_path: Path) -> None:
    db = tmp_path / "w.db"
    for args in (["turn", "x", "y"], ["world-status"], ["dump-context"]):
        result = run_cli([*args, "--db", str(db)])
        assert result.returncode == 1, args
        assert "no world" in result.stderr


def test_kill_and_restart_reproduces_identical_state(tmp_path: Path) -> None:
    """The M2 acceptance: kill the process mid-session; restart; same state.

    Each CLI invocation is a separate process (like `docker exec`), so the
    world must be fully recoverable from the on-disk event log.
    """
    db = tmp_path / "w.db"
    assert run_cli(["new", "Mira", "--hp", "12", "--db", str(db)]).returncode == 0
    for line, narration in (
        ("I follow the road east.", "The road winds into the hills."),
        ("I ask the keeper about the crown.", "She looks away from you."),
        ("I wait for nightfall.", "The lamps go out one by one."),
    ):
        result = run_cli(["turn", line, narration, "--db", str(db)])
        assert result.returncode == 0, result.stderr

    before = run_cli(["world-status", "--db", str(db)])
    assert before.returncode == 0
    before_body = json.loads(before.stdout)

    # "Kill": nothing more to do — the process is already gone (separate
    # subprocess). Restart: a brand-new process reads the same file.
    after = run_cli(["world-status", "--db", str(db)])
    assert after.returncode == 0, after.stderr
    after_body = json.loads(after.stdout)

    assert before_body == after_body, "kill + restart must reproduce the identical state"
    assert before_body["turn"] == 4
    assert before_body["world"]["pc"]["name"] == "Mira"
