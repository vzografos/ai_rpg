"""In-container debug CLI (the documented layout: cli.py).

Deliberately secondary to the web UI (constraint C2) — kept for `docker exec`
debugging. It shares the same provider core and will grow the same commands
as the arbiter's meta verbs (/status /roll /save /load /help) land in M4.

Usage (inside the container):
    uv run python -m rpg.cli status
    uv run python -m rpg.cli ping
    uv run python -m rpg.cli say "what do I do?"
    uv run python -m rpg.cli roll "2d20kh1" [--seed 42]
    uv run python -m rpg.cli new [--name Adventurer] [--hp 12]
    uv run python -m rpg.cli turn "I explore the cave" "You find a dark cave."
    uv run python -m rpg.cli world-status
    uv run python -m rpg.cli dump-context
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from rpg import __version__
from rpg.engine.context import dump_context
from rpg.engine.dice import SeededSource, SystemSource, roll_dice
from rpg.engine.events import Message, WorldDB, WorldEvent, default_db_path
from rpg.engine.sheet import ABILITIES
from rpg.engine.state import WorldState
from rpg.llm import prompts
from rpg.llm.provider import LLMProvider, ProviderConfig, ProviderError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rpg", description="ai_rpg debug CLI")
    parser.add_argument("--version", action="version", version=f"ai_rpg {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="show provider config and version")
    sub.add_parser("ping", help="one-shot LLM round-trip (the M0 hello signal)")
    say = sub.add_parser("say", help="run the M0 one-shot 'turn' with the given player text")
    say.add_argument("text", help="what the player's character does")
    roll = sub.add_parser("roll", help="roll a dice formula headless (no LLM)")
    roll.add_argument("formula", help='e.g. "2d20kh1+3"')
    roll.add_argument("--seed", type=int, default=None, help="seed for a deterministic roll")

    new = sub.add_parser("new", help="create a world (world_init event, committed)")
    new.add_argument("name", nargs="?", default="Adventurer", help="the PC's name")
    new.add_argument("--hp", type=int, default=10, help="starting HP")
    new.add_argument("--db", type=Path, default=None, help="db path (default $RPG_DATA/rpg.db)")
    turn = sub.add_parser("turn", help="commit a headless turn: user line + narration (no LLM)")
    turn.add_argument("text", help="what the player's character does")
    turn.add_argument("narration", help="the final narration for this turn")
    turn.add_argument("--db", type=Path, default=None, help="db path (default $RPG_DATA/rpg.db)")
    for command in ("world-status", "dump-context"):
        entry = sub.add_parser(command, help=f"print the {command.replace('-', ' ')} (no LLM)")
        entry.add_argument(
            "--db",
            type=Path,
            default=None,
            help="db path (default $RPG_DATA/rpg.db)",
        )

    args = parser.parse_args(argv)

    if args.command == "roll":
        try:
            source = SeededSource(args.seed) if args.seed is not None else SystemSource()
            print(str(roll_dice(args.formula, source)))
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0

    if args.command in ("new", "turn", "world-status", "dump-context"):
        try:
            return _world_command(args)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    provider = LLMProvider(ProviderConfig.from_env())
    try:
        if args.command == "status":
            cfg = provider.config
            print(json.dumps({
                "app": "ai_rpg",
                "version": __version__,
                "milestone": "M2",
                "model": cfg.model,
                "base_url": cfg.base_url,
                "api_key_set": bool(cfg.api_key),
            }, indent=2))
            return 0
        if args.command == "ping":
            print(asyncio.run(provider.chat(
                [
                    {"role": "system", "content": prompts.PING_SYSTEM},
                    {"role": "user", "content": prompts.PING_USER},
                ],
                temperature=0.0,
                max_tokens=80,
            )).content or "ok")
            return 0
        if args.command == "say":
            reply = asyncio.run(provider.chat(prompts.action_messages(args.text), temperature=0.9))
            print(reply.content or "(no narration)")
            return 0
    except ProviderError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        asyncio.run(provider.aclose())
    return 2


# --- world commands (M2: no LLM — the engine and the event log only) ---------


def _world_command(args: argparse.Namespace) -> int:
    db_path = args.db if args.db is not None else default_db_path()
    with WorldDB(db_path) as db:
        if args.command == "new":
            if not db.is_empty():
                raise ValueError(f"world already exists at {db_path} ({db.event_count()} events)")
            world = WorldState()
            payload = {
                "pc": {
                    "name": args.name,
                    "hp": args.hp,
                    "max_hp": args.hp,
                    "ac": 15,
                    "abilities": {ability: 10 for ability in ABILITIES},
                    "inventory": {},
                    "gold": 10,
                    "xp": 0,
                },
                "pc_location": "The village square",
                "pc_description": "",
                "locations": [
                    {
                        "name": "The village square",
                        "description": "A quiet square; the tavern smells of woodsmoke.",
                        "exits": [],
                    }
                ],
                "npcs": [],
                "quests": [],
                "items": [],
            }
            world.apply_event("world_init", payload, 0)
            db.commit(world, [WorldEvent(turn=0, kind="world_init", payload=payload)], [])
            print(f"world created at {db_path}: pc {args.name!r} at The village square (turn 1)")
            return 0
        world = db.load_world()
        if world is None:
            raise ValueError(f"no world at {db_path} — create one with: rpg.cli new")
        if args.command == "turn":
            turn = world.turn
            db.commit(
                world,
                [],
                [
                    Message(turn=turn, role="user", content=args.text),
                    Message(turn=turn, role="narration", content=args.narration),
                ],
            )
            last = db.last_snapshot()
            print(
                f"turn {turn} committed: 2 messages, snapshot {last[0]}={last[1][:12]}… "
                f"(world now at turn {world.turn})"
            )
            return 0
        if args.command == "world-status":
            last = db.last_snapshot()
            print(
                json.dumps(
                    {
                        "db": str(db_path),
                        "turn": world.turn,
                        "events": db.event_count(),
                        "last_snapshot": {"turn": last[0], "hash": last[1]} if last else None,
                        "world": world.to_dict(),
                    },
                    indent=2,
                )
            )
            return 0
        if args.command == "dump-context":
            print(dump_context(world, messages=db.messages()))
            return 0
    raise ValueError(f"unknown command: {args.command!r}")


if __name__ == "__main__":
    raise SystemExit(main())
