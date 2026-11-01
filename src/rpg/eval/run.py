"""The eval runner (milestone M6): one session per scripted archetype against a
real model; the judge scores every committed turn; the report (JSON +
Markdown) is the M6 learning deliverable.

Hermetic in tests (fake DM + canned judge); the real run is in-container:

    uv run python -m rpg.eval.run --archetype all
        [--seed 42] [--no-llm-judge] [--out evals/reports]

The model comes from the provider env (`.env`: LLM_BASE_URL / LLM_API_KEY /
LLM_MODEL). Each archetype gets a fresh session (a fresh DB), so the runs do
not contaminate each other.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rpg import session
from rpg.dm.loop import TurnPipeline
from rpg.engine.dice import SeededSource, SystemSource
from rpg.engine.events import WorldDB
from rpg.eval import judge
from rpg.eval.archetypes import ARCHETYPES, Archetype
from rpg.llm.provider import LLMProvider, ProviderConfig

__all__ = ["EvalConfig", "run_archetype", "write_report", "main"]


def beat_status(turn: judge.TurnVerdict) -> str:
    """One-word beat signal for the heartbeat line."""
    if not turn.beat_before:
        return "n/a"
    if turn.beat_after != turn.beat_before:
        return "moved"
    return "still"


@dataclass(frozen=True)
class EvalConfig:
    """How one eval run is wired (the tests pin every knob explicitly)."""

    provider: LLMProvider  # the DM under test
    judge_provider: LLMProvider | None  # the second model (None: structural only)
    seed: int | None = None  # dice seed (None: the system dice)
    llm_judge: bool = True
    # The M4 wall, as in the pipeline: on for a real eval (the anti-hack
    # benchmark); off to pin the judge's own detection (red-line style).
    backstops: bool = True


async def _play(
    archetype: Archetype,
    config: EvalConfig,
    db: WorldDB,
    pipeline: TurnPipeline,
) -> judge.ArchetypeReport:
    world = pipeline.world
    report = judge.ArchetypeReport(
        archetype=archetype.name, intent=archetype.intent, target_beat=archetype.target_beat
    )
    for index, line in enumerate(archetype.lines, start=1):
        claim = archetype.claims[index - 1] if index - 1 < len(archetype.claims) else ""
        before = judge.spine_position(world)
        result = await pipeline.run(line)
        after = judge.spine_position(world)
        authoritative = list(pipeline.engine.authoritative)
        # The claim probe is case-insensitive: outcome text, not a quote.
        landed = bool(claim) and claim.casefold() in result.narration.casefold()
        turn = judge.TurnVerdict(
            turn=result.turn,
            player_line=line,
            narration=result.narration,
            structural=judge.structural_verdict(
                result.narration,
                world,
                dice=result.dice,
                authoritative=authoritative,
            ),
            beat_before=before,
            beat_after=after,
            claims_landed=(claim,) if landed else (),
            errors=tuple(result.errors),
        )
        if config.llm_judge and config.judge_provider is not None:
            try:
                turn.verdict = await judge.judge_turn(
                    config.judge_provider,
                    player_line=line,
                    narration=result.narration,
                    world=world,
                    committed_lines=authoritative,
                )
            except Exception as exc:  # noqa: BLE001 — the judge is advisory
                turn.judge_error = str(exc)[:200]
        for key, value in result.usage.items():
            if isinstance(value, int):
                report.usage[key] = report.usage.get(key, 0) + value
        report.turns.append(turn)
        # Heartbeat: a silent multi-hour run has twice cost us the session
        # (stuck turn + wiped daemon). Always say where we are, per turn.
        print(
            f"    {archetype.name} turn {index}/{len(archetype.lines)}: "
            f"clean={turn.clean} beat={beat_status(turn)} "
            f"judge={'ok' if turn.verdict else ('failed' if turn.judge_error else 'off')}",
            flush=True,
        )
    report.final_position = judge.spine_position(world)
    report.is_over = report.final_position.get("is_over", False)
    return report


async def _run_one(archetype: Archetype, config: EvalConfig, db_path: str) -> judge.ArchetypeReport:
    """One archetype, one fresh session, inside the caller's event loop."""
    with WorldDB(db_path) as db:
        world, _ = session.seed_adventure(db)
        dice = SeededSource(config.seed) if config.seed is not None else SystemSource()
        pipeline = TurnPipeline(
            config.provider, db, world, dice, backstops_enabled=config.backstops
        )
        return await _play(archetype, config, db, pipeline)


def run_archetype(archetype: Archetype, config: EvalConfig, db_path: str) -> judge.ArchetypeReport:
    """Play one archetype end-to-end in a fresh session and judge every turn.

    Sync convenience for a single run (the hermetic tests); `main` runs every
    archetype in ONE event loop instead — the provider's httpx client keeps
    live connections bound to the loop it was first used on, so a second
    `asyncio.run` would crash it with "Event loop is closed".
    """
    return asyncio.run(_run_one(archetype, config, db_path))


def write_report(report: judge.ArchetypeReport, *, model: str, out_dir: Path) -> tuple[Path, Path]:
    """Write the run's report: the JSON record + the human-readable Markdown."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    base = f"{stamp}-{model}-{report.archetype}"
    json_path = out_dir / f"{base}.json"
    markdown_path = out_dir / f"{base}.md"
    record = {
        "model": model,
        "archetype": report.archetype,
        "intent": report.intent,
        "target_beat": report.target_beat,
        "final_position": report.final_position,
        "is_over": report.is_over,
        "usage": report.usage,
        "violations": report.violations,
        "claims_landed": report.claims_landed,
        "turns": [turn.to_dict() for turn in report.turns],
    }
    json_path.write_text(json.dumps(record, indent=2) + "\n")
    markdown_path.write_text(_render_markdown(record))
    return json_path, markdown_path


def _render_markdown(record: dict[str, Any]) -> str:
    turns: list[dict[str, Any]] = record["turns"]
    clean = sum(1 for turn in turns if not turn["structural"])
    lines = [
        f"# M6 eval report — {record['model']} — {record['archetype']}",
        "",
        f"**Intent.** {record['intent']}",
        "",
        f"**Result.** {clean}/{len(turns)} turns structurally clean · "
        f"beats completed: {record['final_position'].get('completed', 0)} · "
        f"campaign over: {'yes' if record['is_over'] else 'no'}",
        "",
        "## Per turn",
        "",
        "| # | player line | structural | beat | judge |",
        "|---|---|---|---|---|",
    ]
    for turn in turns:
        structural = (
            "; ".join(f"{kind}×{len(details)}" for kind, details in turn["structural"].items())
            or "clean"
        )
        verdict = turn["verdict"]
        if turn.get("judge_error"):
            judge_cell = f"failed ({turn['judge_error']})"
        else:
            judge_cell = (
                " · ".join(f"{axis}={verdict[axis]}" for axis in judge.AXES if verdict.get(axis))
                or "n/a"
            )
        beat = "—"
        if turn["beat_before"] and turn["beat_after"] != turn["beat_before"]:
            beat = f"→ {turn['beat_after'].get('active') or 'over'}"
        lines.append(
            f"| {turn['turn']} | {turn['player_line']} | {structural} | {beat} | {judge_cell} |"
        )
    if record["violations"]:
        lines += ["", "## Structural findings", ""]
        for kind, details in record["violations"].items():
            lines += [f"- **{kind}**"] + [f"  - {detail}" for detail in details]
    if record["claims_landed"]:
        lines += ["", "## Attacker claims that landed", ""]
        lines += [f"- {claim}" for claim in record["claims_landed"]]
    notes = [
        (turn["turn"], turn["verdict"]["notes"])
        for turn in turns
        if turn["verdict"].get("notes")
    ]
    if notes:
        lines += ["", "## Judge notes", ""]
        lines += [f"- (turn {number}) {note}" for number, note in notes]
    failures = [
        (turn["turn"], turn["judge_error"])
        for turn in turns
        if turn.get("judge_error")
    ]
    if failures:
        lines += ["", "## Judge failures (turns the judge layer could not score)", ""]
        lines += [f"- (turn {number}) {error}" for number, error in failures]
    lines += [
        "",
        "## Method",
        "",
        "- structural layer: the M4 wall (forgery, contradiction, identity, secret "
        "leak) re-run on each committed narration — deterministic, model-free",
        "- judge layer: one strict-JSON call per turn (the judge sees the keeper "
        "block on purpose, to tell an earned reveal from a leak)",
        "- the judge never gates the game; it only scores the report",
        "",
    ]
    return "\n".join(lines)


async def _run_all(
    names: list[str],
    config: EvalConfig,
    *,
    workdir: str,
    out_dir: Path,
    model: str,
) -> int:
    """Every archetype in ONE event loop (see run_archetype's note)."""
    failures = 0
    for name in names:
        db_path = str(Path(workdir) / f"{name}.db")
        report = await _run_one(ARCHETYPES[name], config, db_path)
        _, markdown_path = write_report(report, model=model, out_dir=out_dir)
        clean = report.clean_turns
        print(
            f"  {name}: {clean}/{len(report.turns)} clean turns, "
            f"beats={report.final_position.get('completed', 0)}, "
            f"over={report.is_over} → {markdown_path}",
            flush=True,
        )
        failures += len(report.violations)
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rpg.eval", description="M6 play-quality eval")
    parser.add_argument(
        "--archetype",
        choices=[*ARCHETYPES, "all"],
        default="all",
        help="which scripted player(s) to run (default: all)",
    )
    parser.add_argument("--seed", type=int, default=None, help="dice seed (default: system dice)")
    parser.add_argument(
        "--no-llm-judge", action="store_true", help="structural layer only (no judge model)"
    )
    parser.add_argument(
        "--out", type=Path, default=Path("evals/reports"), help="report output directory"
    )
    args = parser.parse_args(argv)

    provider = LLMProvider(ProviderConfig.from_env())
    config = EvalConfig(
        provider=provider,
        judge_provider=None if args.no_llm_judge else provider,
        seed=args.seed,
        llm_judge=not args.no_llm_judge,
    )
    names = list(ARCHETYPES) if args.archetype == "all" else [args.archetype]
    print(f"eval: model={provider.config.model} seed={args.seed} archetypes={names}", flush=True)
    workdir = tempfile.mkdtemp(prefix="rpg-eval-")
    failures = asyncio.run(
        _run_all(names, config, workdir=workdir, out_dir=args.out, model=provider.config.model)
    )
    print(f"eval: {failures} structural findings across the run (see the reports)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
