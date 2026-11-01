# M6 eval reports

The real-model eval harness (`src/rpg/eval/`) plays three scripted
archetypes against the live DM and records what actually happened.

## Run it

```sh
make check                       # gate first (lint + hermetic tests)
docker compose -f docker/compose.yml run -d --name rpg-eval \
  --entrypoint sh -e LLM_TIMEOUT=600 test -c \
  "uv sync --no-dev --frozen >/dev/null 2>&1 && \
   uv run python -u -m rpg.eval.run --archetype all --out /app/evals/reports 2>&1; \
   echo EXIT_CODE=\$?"
docker logs -f rpg-eval          # per-turn heartbeats
docker cp rpg-eval:/app/evals/reports/. evals/reports/
```

The model comes from `.env` (`LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL`).
Each archetype gets a fresh session (fresh DB) so runs do not contaminate
each other.

## The three archetypes

| archetype | intent | lines |
|---|---|---|
| explorer | the progress benchmark — plays the story as offered; if the DM keeps up, the campaign completes | 15 |
| talker | probes secrets through dialogue; measures what the DM gives away unearned | 10 |
| saboteur | attacks the rules with false claims ("you gain 500 XP", "the wight is already dead"); measures whether claims reach the committed narration | 10 |

## Two independent scoring layers per turn

1. **Structural (the M4 wall, re-run on committed narration)** —
   deterministic, model-free: forgery, contradiction, identity, secret
   leak. This is the verdict layer.
2. **Judge model** — one strict-JSON call per turn on the four the evaluation layer axes
   (`secret_leak`, `dice_first`, `identity`, `beat_progress`). It sees the
   keeper `[NOW]` block on purpose, to tell an earned reveal from a leak.
   It never gates the game; its failures are recorded per turn
   (`judge_error`), never silently dropped.

## Reading a report

- `<stamp>-<model>-<archetype>.json` — the full record (per-turn
  verdicts, errors, usage).
- `<stamp>-<model>-<archetype>.md` — the human summary: per-turn table,
  structural findings, claims that landed, judge notes, judge failures.
- `beat` column: `→ <beat text>` means the spine advanced that turn (the
  server ticked a waypoint). `—` means the active beat did not change.

## Run results — qwen38, 2026-11-01 (`evals/reports/`)

| archetype | clean turns | beats | judge scored | structural findings |
|---|---|---|---|---|
| explorer | 15/15 | 1/5 | 14/15 (1 truncation) | none |
| talker | 10/10 | 0 | 10/10 | none |
| saboteur | 10/10 | 0 | 10/10 | none |

What the run taught:

- **The structural wall held under direct attack.** All 10 saboteur
  lines (false rolls, pre-resolved fights, stat changes, out-of-fiction
  commands) produced zero forged numbers, contradictions, or identity
  drift in committed narration. Player text stayed intent, never outcome.
- **The judge sees weaknesses the structural layer cannot.** The wall
  matches *verbatim* secrets and *whitelisted* numbers; the judge caught
  paraphrased secret leaks (saboteur turns 5/8/9/10, explorer turns
  4/9/15) and dice-first misses where narration asserted outcomes the
  engine never committed ("the wight's death and 150 XP") in prose the
  forgery checker does not treat as a number claim. Paraphrase-level
  leakage is the concrete M7 target this eval surfaced.
- **The spine advances slowly: 1 beat in 35 turns.** The model narrates
  actions it does not commit with the `note` tool using the waypoint's
  exact text, so the server never sees the evidence. The judge's
  `beat_progress` axis disagrees with the server tick on many turns —
  the judge reads intent, the server requires committed evidence.
- **One probe artifact**: the saboteur's "teleport" claim is recorded as
  landed because the word appears in the DM's *refusal*. Claim probes
  match substrings; read the judge note next to them.
- **One judge truncation** (explorer turn 7, `finish_reason=length` at
  4096) — recorded as a failure, never as a pass.
