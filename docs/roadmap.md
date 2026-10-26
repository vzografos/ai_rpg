# Roadmap — milestones and decisions

This file is the canonical project roadmap: the milestone index, the decision
log, and the verification history. It is the only roadmap the repository
depends on.

> If a decision or reality changes the plan: record it in the Decision log
> below.

## Milestone index

| # | Name | Acceptance in one line |
|---|---|---|
| M0 | Skeleton | `docker compose up` serves the page on :8080 with a working LLM ping; `make check` green; host has only Docker |
| M1 | Deterministic core | 100% engine path coverage; `roll` byte-identical under the same seed; zero LLM |
| M2 | World state, events, context | kill + restart mid-session = identical state; rebuild-from-events property test green |
| M3 | Story spine + DM loop | a human plays 10–20 turns through `shadow_crown` from a browser; fake-LLM 10-turn pipeline test |
| M4 | Backstops + anti-hack | red-line suite fails without the backstops, passes with them; one documented real-model run |
| M5 | Memory + persistence | turn-5 facts still shape turn 50; context stays bounded (measured) |
| M6 | Scoring, conformance, evals, deployment | conformance adventure completes deterministically; honest eval report; clean-machine image |

Cut order if a milestone overruns: M5 → M6 evals. **Never cut M4**
(backstops are load-bearing).

## Decision log

### M0 — default model: strong API for play, Ollama sibling in from day 1 (2026-10-25)

**Decision:** the default model for *playing* is a strong API model
(any OpenAI-compatible endpoint via `LLM_BASE_URL`/`LLM_API_KEY`/
`LLM_MODEL` in `.env` — the shipped `.env.example` points at OpenAI
`gpt-4o-mini` as a cheap default). The **Ollama path is first-class from
day one**: an optional `ollama` sibling service in `docker/compose.yml`,
the same provider code, reachable in-container at `http://ollama:11434/v1`.
With no `.env` at all, the provider falls back to that Ollama sibling.

**Rationale:**
1. The evaluation strategy (the evaluation axes) needs a strong baseline to score
   DM quality against — a weak first model would make "is the architecture
   good" unanswerable. A strong API model for the first playable session
   is the quality signal the project exists to learn from.
2. The provider layer is provider-agnostic by design (the provider layer):
   switching API ↔ local is an env change, zero code — so nothing is
   locked in either direction.
3. The Proxmox LXC deployment target makes local models the end-state for
   privacy/cost (roadmap scope); the Ollama sibling being wired at M0
   means that path is exercised continuously, not discovered at M6.
4. Risk mitigation (roadmap risks, risk 1) is built in: `extract_json`
   ("JSON in prose" + validation) exists in the M0 provider so small
   local models get a usable utility-call path before M3.

**Follow-ups:** record per-model eval results in the M6 report; if the LXC
box gets a GPU, re-run the local-model table with bigger models.

### M1 — deterministic core: engine layout, combat semantics, derived level (2026-10-26)

**Decision:** M1 lands as four headless modules — `engine/dice.py`
(formula grammar `NdS[kh|kl K][±M]`; `RandomSource` protocol whose
`roll(n, sides) -> int` returns the *sum* of n dice, so the parser asks for
one die at a time; `SeededSource`/`SystemSource`; `RollResult` with a
canonical byte-stable `str()`), `engine/sheet.py` (**new module** — not in
the documented layout layout; the character-sheet + XP-table deliverable needed a
home), `engine/rules.py` (`ability_check`/`ability_save`; advantage =
2d20kh1, disadvantage = 2d20kl1, both together cancel; natural 20 always
succeeds, natural 1 always fails), `engine/combat.py` (initiative d20 + Dex
with ties by name; to-hit vs AC; damage dice with crits doubling the dice
once; conditions poisoned/stunned/prone/unconscious; death saves d20 vs 10).
Plus a headless `roll` CLI subcommand (`python -m rpg.cli roll "2d20kh1"
--seed 42`) that makes the byte-identical acceptance demoable in-container
with no LLM.

**Rationale / semantics pinned beyond the M1 bullet list:**
1. **Level is derived from total XP** (`CharacterSheet.level` is a
   property; only `xp` is stored) — the sheet cannot drift from its own
   numbers (hard constraint: the LLM never owns numbers).
2. **5e combat semantics**: a creature at 0 HP is *unconscious* → it
   cannot act (skipped by `end_turn`, `can_act` False, cannot attack);
   *stable* (3 death-save successes) is out of the fight; `Combat.is_over`
   = at most one alive combatant; a natural 20 death save revives at 1 HP.
   The engine is a *total state machine* — every call terminates; whether a
   *played* encounter terminates depends on the playing policy and the
   source (a fixed seed can cycle on the revive-at-1 rule — the
   determinism test uses a terminating seed behind a turn guard).
3. **Web surface deliberately untouched in M1** (headless by plan):
   `/api/status` and the CLI keep the `"M0"` milestone marker until M3
   wires the turn pipeline; the static page's "arrives with M1" placeholder
   texts (dice log, sheet note) get fixed in M3 together with the
   dice/state frames.
4. **No dependency changes.** Coverage gate verified with a one-shot
   `uv run --with pytest-cov pytest --cov=rpg.engine` — 100% of engine
   paths (425/425 statements); `pytest-cov` is not in `pyproject.toml`
   unless the coverage gate becomes permanent (revisit at M6).

**Acceptance verified (2026-10-26):** `make check` green (ruff + 137
tests, 2 skipped); 100% engine statement coverage; `roll` output
byte-identical under the same seed across two separate container runs
(literals pinned in `tests/engine/test_determinism.py`); a scripted
3-combatant encounter resolves entirely by the engine, deterministically.
