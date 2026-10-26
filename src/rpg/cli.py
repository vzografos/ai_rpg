"""In-container debug CLI (the documented layout: cli.py).

Deliberately secondary to the web UI (constraint C2) — kept for `docker exec`
debugging. It shares the same provider core and will grow the same commands
as the arbiter's meta verbs (/status /roll /save /load /help) land in M4.

Usage (inside the container):
    uv run python -m rpg.cli status
    uv run python -m rpg.cli ping
    uv run python -m rpg.cli say "what do I do?"
    uv run python -m rpg.cli roll "2d20kh1" [--seed 42]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from rpg import __version__
from rpg.engine.dice import SeededSource, SystemSource, roll_dice
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

    args = parser.parse_args(argv)

    if args.command == "roll":
        try:
            source = SeededSource(args.seed) if args.seed is not None else SystemSource()
            print(str(roll_dice(args.formula, source)))
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0

    provider = LLMProvider(ProviderConfig.from_env())
    try:
        if args.command == "status":
            cfg = provider.config
            print(json.dumps({
                "app": "ai_rpg",
                "version": __version__,
                "milestone": "M0",
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


if __name__ == "__main__":
    raise SystemExit(main())
