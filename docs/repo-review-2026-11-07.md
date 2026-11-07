# Repository review — 2026-11-07

## Scope and method

A full static review of the current checkout against the canonical roadmap in
`docs/roadmap.md`.

**No tests or containers were run for this review.** Every completion claim
below cites evidence already recorded in `docs/roadmap.md`, `evals/reports/`,
or the committed test suite. The working tree contained one
documentation edit (a "post-v1 target" wording fix in `docs/roadmap.md`) when
the review started; it is preserved.

The review covers: repository layout, every source module, the test suite
inventory, Docker/compose, the adventure content, the eval reports, and every
document that makes a claim about project state.

## Objective

Build a text-only D&D 5e RPG in which an LLM is the Dungeon Master, running
strictly inside Docker and played through a single static web page, with a
persistent world and deterministic rules.

The one-sentence architecture is:

> **The LLM narrates. A deterministic engine adjudicates. A database remembers.**

The project exists to make the classic failure modes of LLM Dungeon Masters
structurally impossible rather than merely discouraged: fudged dice, forgotten
gold, resurrected NPCs, story drift, player-declared outcomes, and keeper-secret
leaks.

## Architecture as built

| Layer | Modules | Responsibility |
|---|---|---|
| Deterministic engine | `engine/dice.py`, `sheet.py`, `rules.py`, `combat.py`, `state.py`, `events.py`, `context.py`, `backstops.py` | Dice, SRD 5.1 checks/combat/sheets, validated state transitions, append-only event log, context assembly, structural anti-hack checks. No LLM decisions. |
| Story / DM orchestration | `dm/spine.py`, `tools.py`, `loop.py`, `memory.py`, `arbiter.py` | Authored story spine and beat ticks, the DM tool surface, the turn pipeline, memory folding/recall, meta-vs-in-game arbitration. |
| LLM access | `llm/provider.py`, `llm/prompts.py` | OpenAI-compatible transport, usage metering, JSON extraction, every prompt template. |
| Persistence / sessions | `session.py`, `engine/events.py` | One live SQLite campaign plus named, self-contained saves; `/new`, `/save`, `/load`. |
| Transports | `web/app.py`, `web/static/index.html`, `cli.py` | FastAPI + turn-level SSE; one static vanilla-JS page; in-container debug/play CLI. |
| Evaluation | `eval/archetypes.py`, `eval/judge.py`, `eval/run.py`, `evals/reports/` | Scripted explorer/talker/saboteur runs, structural re-scoring, advisory judge, JSON+Markdown reports. |
| Content | `content/shadow_crown.json` | Pre-authored adventure: spine, locations, NPCs, encounters. |

The load-bearing invariant holds in the code: the LLM mutates the world only
through validated tool calls; each tool dispatches to
`WorldState.apply_event`, which clamps and rejects invalid transitions; the
event log is the authority and the SQLite tables are a materialized view; the
narration is rendered from committed state. The M4 backstops add a structural
wall over the narration itself.

## Completion status

| Milestone | Status | Evidence |
|---|---|---|
| **M0 — skeleton** | **Complete** | Docker image, FastAPI + static page, provider ping, in-container tests. `make up` on :8080 re-verified 2026-11-01 after the compose profile fix. |
| **M1 — deterministic core** | **Complete for v1 scope** | Dice, checks, combat, XP/level, single-class spell slots; 100% engine statement coverage enforced by `make test`/`make check`. Spell lists/cast effects, subclasses, and multiclassing remain out of scope. |
| **M2 — world state, events, context** | **Complete** | Event-log authority, replay/rebuild property test, kill+restart byte-identical check, bounded context builder. |
| **M3 — story spine + DM loop** | **Complete** | 10-turn fake-LLM web-pipeline session; live HTTP 13-turn run; human browser acceptance completed at 19 committed actions (UI counter 20) with no crash. |
| **M4 — backstops + anti-hack** | **Complete** | Red-line suite passes with the wall and fails without it; documented qwen38 real-model run: leakage 0/7, misses 0. |
| **M5 — memory + persistence** | **Implementation complete; long-run real-model acceptance open** | 52-turn fake-LLM memory test and save/load round-trip pass. A 30-turn qwen38 probe exposed truncation, a stuck summary watermark, and 116 `fact_add` events (111 duplicate triples) on turn 27. The 50+ turn real-model quiz was not attempted. |
| **M6 — scoring, conformance, evals, deployment** | **Complete (2026-11-01)** | Conformance adventure; committed qwen38 eval reports; hardened read-only image; clean-machine acceptance (tarball → fresh `docker load` → bare read-only run with one live LLM turn); LXC runbook. |

**Bottom line:** v1's planned surface (M0–M4, M6) is complete. M5's *code* is
complete, but its defining real-model guarantee — a turn-5 fact still shaping
turn 50 with a bounded context and a moving summary — is only proven against
the fake LLM. That is the one material completion gap.

## Recorded verification ledger

These are the latest recorded results, not re-runs:

- **Gate:** `make check` in Docker, 2026-10-31 — Ruff clean, **409 passed,
  2 skipped, 100% engine statement coverage**. The two skips are repo-doc
  layout checks intentionally excluded from the test image (`tests/test_skeleton.py`).
- **M4 real-model red-line:** qwen38, seven attacks, fresh session per attack —
  no forbidden fragment in any committed narration, zero whole-wall violations.
  The wall fired once (identity drift in the jailbreak narration) and a clean
  line committed after one re-ask.
- **M6 live eval (qwen38, 2026-11-01):** 35/35 turns structurally clean;
  judge scored 34/35 (one `finish_reason=length` truncation recorded as a
  failure). Explorer 15/15 clean, 1 beat advanced; talker 10/10; saboteur
  10/10; zero structural findings across all three.
- **M6 clean-machine acceptance:** image tarball → fresh `docker load` → bare
  `docker run --read-only` on a fresh volume with page/status/seed plus one
  live LLM turn. Found and fixed the image-CMD `uv run` writable-cache defect.
- **M5 30-turn probe:** documented above; not an acceptance pass.

## What is strong

1. **The central invariant is real, not aspirational.** State changes really do
   go through one validated writer; the LLM has no direct database handle; the
   narration is post-commit. The red-line suite demonstrates each backstop
   earns its keep by failing when that check is removed.
2. **The event log design is disciplined.** Snapshots, contiguous sequence
   checks, rebuild-from-events, and replay verification make corruption
   detectable instead of silently playable.
3. **The anti-hack layer is structural and bounded.** Six pattern-based checks,
   at most three Stop-form re-asks, then a deterministic fallback: a turn is
   never lost and the model never sees internal check names.
4. **Memory is bounded by construction.** Watermark-based folding, FTS5 recall,
   a recent-fact tail, and a metered context builder — with tests for the
   fold-overflow, empty-summary, and restart paths.
5. **Sessions are simple and safe.** One live DB plus self-contained named
   saves; the SQLite online-backup API means a save is an independent campaign.
6. **The eval harness separates verdict from opinion.** The structural wall is
   the verdict; the judge is advisory and its failures are recorded per turn
   rather than silently converted to passes.
7. **The deployment story is tight.** Same image for dev and prod, read-only
   rootfs, non-root, no bind mounts, DB on the only writable volume, and an
   acceptance run that actually caught a read-only defect.
8. **Tests are hermetic.** No network in the suite; fake provider or
   `httpx.MockTransport`; seeded dice for determinism.

## Findings

### Documentation drift (fixed in this review)

- **`README.md`** claimed M3's human session was still pending, understated M4,
  and marked M6 partial; its layout omitted `sheet.py`, `memory.py`,
  `session.py`, `eval/`, `tests/conformance/`, `tests/eval/`, `docs/`, and
  `evals/`; it named `tests/loop/` instead of `tests/dm/`. Updated to the
  2026-11-07 state.
- **`tests/__init__.py`** described a `tests/loop/` directory that no longer
  exists; the docstring now lists the actual suites (`engine`, `dm`, `web`,
  `llm`, `redline`, `conformance`, `eval`).
- **`evals/README.md`** still called paraphrase leakage an "M7 target" and its
  run command omitted the `test` profile. Updated to "post-v1 target" and a
  corrected command.
- **`docs/lxc-runbook.md`** contained an invalid `... pull && up -d prod`
  shell command and copied `compose.yml` to a path that broke its relative
  `env_file`/build context. Corrected the layout and the update flow.
- **`src/rpg/cli.py`** said the meta commands "will land in M4" although they
  already exist. Docstring corrected.
- **`AGENTS.md`** listed an incomplete SSE frame grammar (missing
  `quest`/`beat`) and had no docs-sync or review-only rule. Both added.
- **`src/rpg/content/shadow_crown.json`** carried `"milestone": "M3"`.
  Updated to `M6` (the adventure is now also the M6 conformance adventure).
- **`Makefile`** documented `make pull-model` as `ARG=model` while the target
  reads `$(model)`. Corrected.

### Code / design findings (not changed by this review)

1. **Layering inversion — medium.** `engine/state.py` and `engine/events.py`
   import `StorySpine` from `rpg.dm.spine`, and `engine/context.py` imports
   `rpg.llm.prompts`. The deterministic engine therefore depends on the DM and
   LLM layers. There is no runtime defect today, but it makes the engine harder
   to reuse or test in isolation and creates a future import-cycle hazard.
   Recommended post-v1 fix: move the spine model into `engine/` (or a neutral
   module), and pass prompt text into the context builder instead of importing
   prompt templates.
2. **M5 real-model memory behavior — medium (product).** The 30-turn probe
   showed repeated fact-extraction truncation, a summary watermark stuck at 0,
   and duplicate `note` emissions (116 events, 111 duplicate triples). The
   bounded-context mechanics are tested; the model's *use* of them at 50+ turns
   is not. This is the main open acceptance item.
3. **Eval runner always exits 0 — low (tooling).** `src/rpg/eval/run.py:296`
   prints the structural-finding count and returns 0 even when findings exist.
   That is consistent with the report-only design, but it makes the harness
   unsuitable as a CI gate. Recommended: add `--strict` (or document the
   report-only contract prominently).
4. **Judge axis values are not validated — low (data quality).**
   `judge_turn` (`src/rpg/eval/judge.py:170`) keeps only the four known keys
   but accepts any values. A malformed judge response is recorded verbatim.
   The prompt enumerates the allowed values; validating them would make report
   aggregation trustworthy.
5. **Dead prompt helpers — low (cleanup).** `FINALIZER_USER`, `opening_user`,
   `SPINE_GEN_SYSTEM`, and `spine_gen_user` in `llm/prompts.py` have no
   callers; `action_messages` is used only by the M0 `say` CLI path. Either
   wire them to the generated-spine/finalizer paths or remove them so the
   prompt module reflects the live surface.
6. **`spine_now=None` type mismatch — low.** `dm/loop.py:231` passes `None`
   to `build_context(spine_now: str = "")`. It is harmless because the builder
   tests truthiness, but it defeats the type hint. Pass `""` instead.
7. **Formatting is not gated — low (process).** `ruff format` is not part of
   `make check`; the roadmap records 23 pre-existing unformatted files. Decide
   gate-or-ignore before post-v1 work so the choice is intentional.

## Open risks and recommended post-v1 order

1. **Close M5's real-model acceptance.** Fix the `note` duplication and
   summary/fact truncation first, then run the 50+ turn qwen38 memory quiz
   with an explicit turn-50 recall assertion.
2. **Address the three eval-surfaced behavior gaps:** paraphrased secret leaks,
   prose dice-first misses, and the `note`-commit gap (the DM narrating
   uncommitted actions, leaving the spine at 1 beat in 35 turns).
3. **Repair the layer boundary** (engine must not import `dm`/`llm`).
4. **Tighten the eval harness:** validate judge values; add an opt-in strict
   exit code.
5. **Make formatting a decision:** gate `ruff format` or document the ignore.
6. **Remove or wire dead prompt code**, and fix the `spine_now` hint.

## Documentation map

- `README.md` — project overview, current state, quickstart, layout, usage.
- `docs/roadmap.md` — in-repo milestone index and decision log.
- `docs/repo-review-2026-11-07.md` — this review.
- `docs/lxc-runbook.md` — Proxmox LXC deployment.
- `evals/README.md` — running the eval harness and reading reports.
