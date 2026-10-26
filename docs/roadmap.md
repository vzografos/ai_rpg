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

### M2 — world state, events, context: log is the authority, tables are a view (2026-10-26)

**Decision:** M2 lands the state/memory layout state layer as three engine modules.
`engine/state.py` — the entity records (`PlayerCharacter` wrapping the M1
`CharacterSheet`, `Npc`, `Item`, `Quest`, `Location`, `Fact`,
`CombatantRecord`, `CombatState`) plus `WorldState.apply_event(kind,
payload, turn)`, the **single validated, clamped writer** (every event kind
checks names/ranges/known-exits/known-combatants and rejects with
`ValueError`; no other code path mutates the world). `engine/events.py` —
the SQLite schema (state/memory layout: `pc`, `npc`, `item`, `quest`, `location`, `fact`,
append-only `event`, per-turn `snapshot`, `message` with role restricted to
user/narration), `WorldDB` and `rebuild_world`. `engine/context.py` — the
context builder (stable head + volatile tail + trimmed transcript +
`Context`/`ContextMeter` + `dump_context`).

**Semantics pinned beyond the M2 bullet list:**

1. **The event log is the authority; the entity tables are a materialized
   view.** `WorldDB.commit(world, events, messages)` is one transaction
   (BEGIN IMMEDIATE → insert events → resync tables → insert the
   per-turn snapshot → insert messages → COMMIT) and then `world.turn +=
   1`; the contract is that the caller has *already* applied the events to
   the live world. `load_world` replays the log, sets `turn` to the
   snapshot count, verifies the replay against the last committed snapshot
   (corrupt log → `WorldCorruptionError`, no world), and resyncs the
   tables. This resolves the M1 follow-up: combatant/sheet mutations are
   now log-authoritative via the `pc_*` and `combat_*` events.
2. **Combat lives only in the log** — there is deliberately no `combat`
   table. `combat_begin/attack/turn/condition/heal/end` carry *committed
   results* (M1's rolls, initiative order, HP, death-save counters);
   replay applies them, it never re-rolls. The M1 `Combat` object stays
   in-memory for play; the M3 loop will bridge (the property test already
   does exactly that bridging with the real M1 `Combat` as the source of
   the committed payloads). Replay backstops mirror M1: an attack requires
   a living, acting attacker; a dead defender is an error; a stable
   defender is a no-op; the nat-20 revive is only legal at exactly 1 HP
   with (successes, failures) = (1, 0); 0 HP requires a committed death
   save, never (3, 3).
3. **Snapshots name the world at the committed turn** (fingerprint taken
   before the turn increment); a no-op turn (e.g. a missed attack) may
   legitimately re-name the identical state — the guarantee is presence
   per turn, not distinctness. The log must be contiguous from seq 1
   (`rebuild_world` rejects gaps/duplicates).
4. **Context builder** per state/memory layout: stable head = DM identity + 5e rules
   summary + GM standards + keeper discipline (templates in
   `llm/prompts.py`, the only home for prompts). Volatile tail = the state
   block headed *"Current game state (engine-provided, not spoken by any
   player)"* (location/exits, sheet, NPC roster, quests, items, facts,
   combat) + the trimmed transcript (last 12 lines, trim-oldest-until-fit,
   floor of 2) + the player line. Keeper-only data (NPC agendas) must
   never enter the tail — pinned by test. Metering is the char heuristic
   `ceil(len/4)` with a default budget of 8000 tokens (a real tokenizer is
   an M5 upgrade); `dump_context` / `GET /api/dump-context` /
   `rpg.cli dump-context` print the metered layout.
5. **Milestone marker moved to "M2" now** (the M1 log said it would wait
   for M3): the world-state endpoints (`GET /api/world`) and the
   dump-context surface are M2 deliverables by milestone M6; the turn pipeline, dice
   frames and the static-page placeholders remain M3.
6. **M2 CLI** (`rpg.cli new [name] [--hp] [--db]`, `turn TEXT NARRATION`,
   `world-status`, `dump-context`) is a headless, no-LLM commit surface:
   `turn` records the user line + narration and commits a zero-event turn
   (snapshot still names the state); real events arrive with the M3
   pipeline.

**Acceptance verified (2026-10-26):** `make check` green (ruff + 222
tests, 2 skipped). The M2 property test
(`tests/engine/test_events_property.py`) plays 40 turns × 5 seeds of
random operations — sheet/entity/fact mutations and combat bridged
through the *real* M1 `Combat` — through `WorldDB.commit`, then asserts
three views of the same world agree: the materialized tables
(`load_from_tables`), the pure event-log replay (`rebuild_world`), and a
restart on the same file (`load_world`, snapshot-verified); plus
"every committed turn carries a snapshot". Kill+restart also verified
live: on the dev container, `rpg.cli new` + two committed turns,
`docker compose stop app` (mid-session kill), `start`, and
`world-status` output byte-identical before/after.
