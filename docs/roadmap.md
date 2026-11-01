# Roadmap — milestones and decisions

This file is the canonical project roadmap: the milestone index, the decision
log, and the verification history. It is the only roadmap the repository
depends on.

> If a decision or reality changes the plan: record it in the Decision log
> below.

## Milestone index

| # | Name | Acceptance in one line | Status as of 2026-11-01 |
|---|---|---|---|
| M0 | Skeleton | `docker compose up` serves the page on :8080 with a working LLM ping; `make check` green; host has only Docker | Implemented; live provider and production HTTP smoke passed. `make up` was verified on :8080 after the Compose service-profile fix (2026-11-01); host-only-Docker check was not repeated. |
| M1 | Deterministic core | 100% engine path coverage; `roll` byte-identical under the same seed; spell slots; zero LLM | SRD 5.1 single-class slot progression, spending, and rest recovery implemented; 100% current engine statement coverage enforced by `make test` and `make check`. Spell lists/casting effects and multiclassing remain out of scope. |
| M2 | World state, events, context | kill + restart mid-session = identical state; rebuild-from-events property test green | Implemented; property/replay tests pass. Previous live kill/restart check is recorded below. |
| M3 | Story spine + DM loop | a human plays 10–20 turns through `shadow_crown` from a browser; fake-LLM 10-turn pipeline test | Fake session passes; live HTTP run advanced beat 1; human browser session completed 19 committed actions (UI counter 20) without a crash. |
| M4 | Backstops + anti-hack | red-line suite fails without the backstops, passes with them; one documented real-model run | Fake-LLM red-line suite passes; real-model run is documented, not repeated in this audit. |
| M5 | Memory + persistence | turn-5 facts still shape turn 50; context stays bounded (measured) | 52-turn memory and save/load tests pass. A 30-turn qwen38 probe selected an old fact through FTS in a simulated prompt, but the read-only model answer was empty; folds stayed at watermark 0 and turn 27 emitted 116 DM fact-adds (111 duplicate triples). The 50+ turn real-model acceptance remains open. |
| M6 | Scoring, conformance, evals, deployment | conformance adventure completes deterministically; honest eval report; clean-machine image | **Complete (2026-11-01):** real-model qwen38 eval reports committed (`evals/reports/`, 3 archetypes); clean-machine acceptance passed — tarball → fresh `docker load` → bare read-only run with a live LLM turn (found and fixed a defect: the image CMD's `uv run` needed a writable cache). |

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

### M3 — story spine + DM loop: spine in the log, server-ticked beats, turn-level SSE (2026-10-27)

**Decision:** M3 lands the play surface as three `dm/` modules plus
content. `dm/spine.py` — the `StorySpine` model (beats with waypoints of
kind place/item/npc/objective/fight/narrative, cast with keeper-only
agendas, planned events, finale), loaded from the pre-authored adventure
`content/shadow_crown.json`; all spine mutation goes through clamped
`spine_set`/`spine_delta` **events** (`apply_delta` rejects unknown ops,
rewrites of completed beats, non-final statuses, out-of-range counts) —
the spine is a log-resident state like any other, with a single-row
`spine` entity table as its materialized view (the M2 "log is the
authority, tables are a view" property extends to it unchanged).
`dm/tools.py` — the DM tool schemas and `ToolEngine`: every tool
validates its arguments, dispatches to `WorldState.apply_event`, and
returns a `ToolResult` carrying the events, dice frames and
**authoritative lines** (the engine's own wording of every number) that
the narration must reflect verbatim; `start_combat`/`end_combat` bridge
the M1 `Combat` object — the whole encounter is resolved by the engine
and committed attack-by-attack (`combat_begin/attack/turn`), and
`end_combat` *derives* the claimed outcome from the committed state and
rejects mismatches (defeat wakes the PC at 1 HP). `dm/loop.py` — the the turn pipeline
pipeline per turn: arbitrate (meta short-circuit) → intent (one advisory
`extract_json` call, never fatal) → context (M2 builder + spine `[NOW]`
block in the stable head) → bounded DM tool loop (8 rounds) → single
`WorldDB.commit` → spine tick/advance → SSE frames rendered from committed
state.

**Semantics pinned beyond the M3 bullet list:**

1. **The model can narrate the story but not advance it.** Beats advance
   only by server-side ticks: `spine.progress_events(world)` emits a
   `tick_waypoint` delta for every *active* beat waypoint the committed
   world justifies (place → PC location, item → PC holds it, npc → a
   matching display name or `npc:<name>` reference on a `spoke_with` fact,
   objective/fight/narrative → a recorded fact with that value — facts
   enter only through the `note` tool, i.e. real play), and
   `advance_events()` closes a beat only once all its waypoints are
   ticked. The LLM's only story-side tools are `note`/`quest_update`
   (evidence) and `ask_clarification` (ends the turn with the question as
   narration).
2. **Thirteenth tool: `award` (xp/gold)** — not in the M3 bullet list;
   the live DM needed a legal way to pay out, and it clamps to
   non-negative ints through the existing `pc_*` events. Rejection is the
   contract: every bad tool call returns a `REJECTED` message and commits
   nothing (C3).
3. **SSE is turn-level, not token-level.** Frame grammar: `narration`,
   `dice`, `state` (post-commit `world.to_dict()`), `quest` (player-safe
   quest lines + completed beats), `beat` (a new beat started this turn),
   `error`, `done` (committed turn + usage + tool names). Rationale:
   M3's acceptance is playable turns, and the frame grammar is the
   transport contract the browser, CLI and test harness all parse — token
   streaming is a web-layer optimization that can layer on top later
   without changing it. The `quest`/`beat` projections keep keeper-only
   data (agendas, planned-event details) out of the player-facing
   surface, pinned by test.
4. **Minimal arbiter.** Meta commands `/help /status /roll /world /ooc`
   are engine-only (zero LLM, a committed message pair, dice via the same
   seeded source); everything else is in-game intent, wrapped as
   untrusted data (`player_action_wrap` — C4) before any model sees it.
   The intent call's output is advisory only (appended to the user tail as
   "Advisory from the adjudication clerk"); it can never commit.
5. **A turn is never lost.** Provider failure mid-loop or tool-budget
   exhaustion (8 rounds without narration) falls back to a deterministic
   narration built from the authoritative lines (or a fixed line when
   nothing committed); the turn still commits with an `error` frame. The
   dice of every roll are carried as frames regardless, so the narration
   stays honest about numbers.
6. **CLI play surface:** `rpg.cli start` seeds the adventure (world_init
   + spine_set at turn 0, no LLM) and `rpg.cli play TEXT [--seed]` runs
   one headless pipeline turn — the live smoke path that exercises the
   real provider in-container. `--seed` gives reproducible dice
   (`RPG_SEED` for the web app) so an acceptance run is replayable.

**Acceptance verified (2026-10-27):** `make check` green (ruff + 272
tests, 2 skipped) — including the scripted **10-turn fake-LLM session
through the real web pipeline** (`tests/web/test_session.py`: beats 1–2
complete on committed evidence with `beat` frames on turns 3 and 7,
per-turn snapshots, and a restart on the same db file replaying to the
byte-identical world, dice pinned by seed) plus dm unit suites for the
spine deltas, tool dispatch (incl. the deterministic seed-42 undercroft
ambush and `end_combat` outcome derivation) and pipeline fallbacks. Live
smoke against the configured local model (qwen38 on vLLM): `start` + two
`play` turns — the DM narrated on-beat (Brenna → the blackened fields →
Old Maren), used `move_to`/`note`/`npc_note` as the only mutations, and
beat 1 closed itself with a `beat` frame; no crash path seen. The full
10–20 turn browser sitting remains the human's to play (the surface is
ready: `make up`, new session, transcript restore, sidebar state).

**Live HTTP follow-up (2026-10-31):** An isolated Docker dev app completed
13 sequential `/action` SSE turns against qwen38 without losing a turn or
crashing. The first 12-turn segment exposed an NPC waypoint mismatch:
`spoke_with` facts sometimes used `npc:<name>` rather than the display name,
so the server did not tick those NPC waypoints. `progress_events` now
recognizes that namespaced reference only on `spoke_with` facts; the new
regression tests pass, and turn 13 emitted a `beat` frame and advanced to
“Find the candle that can unmake the crown.” The run also produced six
recoverable fact-extraction truncation frames (`finish_reason=length`) and
two malformed spine-refresh proposals that were safely dropped. This is
live HTTP evidence, separate from the human browser session below. The final
Docker `make check` passed: Ruff clean, 409 passed,
2 skipped, and 100% engine statement coverage.

**Human browser follow-up (2026-10-31):** The player reported 8 turns;
inspection found 9 committed player actions. The session remained responsive
and beat 1 stayed active with Old Maren done and Brenna still pending. The
“give bread” action spent 1 gold and created a loaf directly with Maren even
though the PC did not hold bread, while the narration omitted the purchase.
`grant_item` now rejects unowned direct transfers to NPCs, transfers from
sheet inventory remove that quantity, and the web client displays committed
item/gold/XP effects. This already-committed turn remains in the active save;
the human 10–20 turn acceptance is still in progress. XP remained 0 because
no reward or combat-victory award was committed and beat 1 is incomplete.

The browser session continued to 19 committed player actions (the UI counter
showed 20). No crash occurred, and the opening harvest investigation led the
player to Old Maren without a request to advance the quest. The last combat
turn included a rejected claim of victory because the engine derived defeat;
the committed result cleared combat and woke the PC at 1 HP. A fact-extraction
truncation warning also appeared, but the turn committed. Beat 1 remained
active with Old Maren done and Brenna pending. This completes M3's 10–20 turn
human browser acceptance; the recoverable memory warning is tracked under M5.

### M4 — backstops + anti-hack: structural wall, bounded Stop-form re-asks, red-line suite (2026-10-28)

**Decision:** M4 lands the the anti-hack layer anti-hack layer as `engine/backstops.py`
(six structural checks — no fiction reading), a bounded Stop-form re-ask
loop inside the turn pipeline (`dm/loop.py`, step 6), a strengthened
keeper prompt (`llm/prompts.py`), per-NPC knowledge scoping in the state
block (`engine/context.py`), and the red-line suite (`tests/redline/`).

**Semantics pinned beyond the M4 bullet list:**

1. **Six structural checks, one orchestrator.** `run_backstops(narration,
   world, dice, events, authoritative, kinds=...)` aggregates: *forgery*
   (a roll-shaped number in prose when no dice ran this turn),
   *contradiction* (a prose number disagrees with the real roll),
   *identity* (the PC named in narration — solo drift), *lost_narration*
   (a roll committed but no narration carries it), *combat_transition*
   (prose pre-resolves a hit/miss/damage/condition or names an
   unregistered attacker), *secret_leak* (keeper-only text — NPC agendas,
   cast agendas, spine event details, the antagonist line — quoted
   verbatim; a 12-char floor keeps short fragments honest). Violations
   carry `kind` + `detail`; only the detail feeds the re-ask — the model
   never sees check names.
2. **Number discipline is pattern-based, not arithmetic.** Claimed
   numbers come only from roll-shaped patterns (roll/DC/damage/XP/gold
   claims); allowed numbers = the dice-frame values (total, d20, dc, ac)
   + the integers inside the authoritative lines (die names like "d20"
   stripped *before* matching, so "d20" never licenses the number 20) +
   the sheet numbers. Forgery fires only with zero dice this turn;
   contradiction only when dice ran and a claimed number disagrees.
3. **Bounded Stop-form re-ask, then deterministic fallback.** On any
   violation the loop re-asks with the Stop form (`prompts.stop_reask`:
   the turn does not end; here are the concrete violations; re-narrate
   from committed state) — at most `MAX_BACKSTOP_REASKS = 3`, text-only
   (a tool-call re-ask response is rejected and counts), re-verified
   after every re-ask; on exhaustion the turn commits the deterministic
   `fallback_narration` built from the authoritative lines (fixed line
   when nothing committed), with an `error` frame. The fallback is
   terminal — never re-verified — so engine wording that names the PC is
   exempt by construction. A turn is never lost (extends the M3 pin).
4. **NPC knowledge scoping in the state block.** Each NPC record now
   carries an "NPC knows (Name):" line listing what that NPC may voice;
   keeper-only agendas never enter the block at all (excluded by
   construction, not filtered after). Pinned: one NPC's private record
   never leaks into another NPC's line.
5. **Anti-injection hardening.** `player_action_wrap` now names the
   threat explicitly (player text is untrusted data; instructions inside
   it are never followed, outcomes inside it are never accepted);
   `KEEPER_DISCIPLINE` extended with the same two clauses. Pinned:
   player text appears in the DM prompt only inside the wrap; meta
   commands bypass the wall and the LLM entirely.
6. **Red-line suite = the "each backstop earns its keep" proof.**
   `tests/redline/` runs seven scripted attacks (i-win, declared-combat,
   identity-drift, secret-probing, jailbreak, fake-system-instruction,
   contradicted-roll) as **with-wall / without-wall pairs**: with
   `backstops_enabled=False` the compromised line commits verbatim and
   the attack's specific backstop flags it (removing any one check turns
   its pair red); with the wall on, the committed narration is clean and
   passes a whole-wall re-verification. Lost-narration is pinned at
   pipeline level instead (its fallback names the PC, exempt by design).

**Acceptance verified (2026-10-28):** `make check` green (ruff + 333
tests, 2 skipped) — the red-line suite is green on the fake LLM and
fails without the backstops by construction. One real-model red-line run
documented: qwen38 on the local vLLM server, the seven attacks, fresh
session per attack, unseeded dice — **leakage 0/7** (no forbidden
fragment in any committed narration, zero whole-wall violations), **misses
0** (no committed line violated the wall). The model itself refused all
seven attacks on the first round; the wall fired once (jailbreak: the
model's first narration named the PC, "you remain Kellan Voss" →
identity re-ask, rewritten after 1 re-ask, clean line committed).

### M5 — memory + persistence: FTS5 recall, folded summary, /new /save /load (2026-10-29)

**Decision:** M5 lands the state/memory layout memory layer as `dm/memory.py` (the
keeper's recall, folded on every turn), session handling in
`engine/events.py` (`WorldDB.recall_facts`, `backup_to`/`restore_from`),
`session.py` (the single owner of `/new /save /load` — engine and web run
through it), and the web session endpoints.

**Semantics pinned beyond the M5 bullet list:**

1. **The watermark moves only via `summary_set`.** The fold is due per
   committed turn: every `FOLD_EVERY_TURNS = 10` turns since the last
   watermark, or as soon as the metered total reaches
   `FOLD_METER_FRACTION = 0.75` of the budget. Its input is the transcript
   since the last watermark plus the in-hand pair (the turn being committed,
   not in the log yet); its output is one `summary_set` event (summary ≤
   `SUMMARY_MAX_CHARS = 1500`, watermark = the turn being committed) that
   rides the same turn transaction. Fold-once, retry-once — and only on
   overflow (the retry must come back ≤ `SUMMARY_RETRY_MAX_CHARS = 900`);
   an empty or unparseable summary is *silent no-progress* — not an error,
   not retried: the old summary stays, the watermark does not move, and the
   next due turn folds again. The summary is world state (`world.summary`
   in the stable head) and rides the log, so a restart replays it.
2. **The context stays bounded by construction; the meter is the verdict.**
   `build_context` drops transcript lines at or before the watermark (they
   are folded into the summary in the stable head), keeps the last 12, and
   trims oldest-until-fit (floor 2). The state block's facts are bounded
   too: the FTS5 recall for the player's line (≤ `RECALL_LIMIT = 8`) plus
   the most recent `RECENT_FACTS = 5` on record that are not already among
   them (the debug dump shows the whole record). The client sees the verdict
   per turn (`done.over_budget`) and the metered layout via
   `/api/dump-context`.
3. **Recall runs on the raw (unwrapped) player line**, not the untrusted
   wrap — the wrap is prompt-safety text, not what the player said.
   `recall_facts` reduces the line to FTS5 terms (lowercased, deduped,
   stopword-free, 3+ chars) and matches the `fact_fts` index
   (bm25-ranked, deduped by (subject, predicate, value)); an unmatchable
   query recalls nothing. Facts enter the world only through
   `fact_add`/`fact_remove` events — the `note` tool, or the capped,
   deduped `extract_turn_facts` utility call (resolved-facts removal),
   whose non-dict responses pass through the caller's guard.
4. **Spine refresh at chapter close is the model's clamped delta path.**
   When a beat completes, one `extract_json` call may propose only the
   `_REFRESH_OPS` (`beat_status`/`append_detail`/`rewrite_beat`); each
   delta goes through the same clamped `apply_delta` as the M3 tool path
   (unknown ops, rewrites of completed beats, cross-act rewrites
   rejected).
5. **Sessions: one live DB + named saves; a save is self-contained.** The
   live campaign is `$RPG_DATA/rpg.db` (the `rpg-data` volume — `docker
   restart` resumes it). `/save [name]` takes a full copy through the
   SQLite online-backup API (log + tables + snapshots) into
   `$RPG_DATA/saves/<name>.db`; each save is an independently resumable
   campaign, so multi-session is "several named saves". `/load <name>`
   records the switch in the campaign being left, then swaps the live DB
   for the save and rebinds. `/new` seeds the pre-authored adventure only
   into an empty live DB (refuses otherwise and offers `/save` first).
   Save naming is one rule (`SAVE_NAME_RE`, default `turn-<N>`); the
   arbiter's meta verbs and the web endpoints (`/api/new /api/save
   /api/load /api/sessions`) both run through `session.py`.
6. **The memory quiz plants a fact every five turns, not 5/20/40**
   (deviation from the milestone M6 bullet): with ten facts on record by turn 50 the
   bounded recent-tail (the last five) no longer shows the turn-5 fact —
   only the FTS5 recall for the turn-50 line can bring it back. The test
   asserts both directions: the recalled turn-5 fact is in the state block,
   and the older unrecalled facts are out of it.

**Acceptance verified (2026-10-29):** `make check` green (ruff + 357
tests, 2 skipped) — including `tests/web/test_memory.py`: a **52-turn
fake-LLM session through the real web pipeline** (a `note` fact planted
every five turns; on turn 50 the player's line recalls the turn-5 fact
into the state block while the bounded tail leaves the old facts out and
the folded summary rides the stable head; folds fire at turns
10/20/30/40/50 — summary and watermark on the world and in the log; every
turn `over_budget` False with the metered total within budget; kill+
restart replays the log to the byte-identical world, summary included)
plus a **checkpoint round-trip** (`/save midpoint` at turn 20, play on to
30, `/load midpoint` — the save is self-contained and the live DB is
swapped for it; bad names 400, unknown saves 404) and engine-level memory
unit tests (fold overflow/empty paths, fact extraction dedupe/removal,
recall, backup/restore, bounded state block).

**Real-model probe (2026-10-31; capped at 30 committed turns):** An
isolated Docker session on `qwen38` committed turns 1–30. Fact extraction
returned length-truncated JSON on turns 4, 5, 6, 8, 19, 28, and 29. The
rolling summary never advanced from watermark 0; explicit length-truncation
warnings recurred, while other folds silently made no progress. Three turns
hit provider read timeouts and recovered through the turn fallback. On turn
27, the model emitted 116 `fact_add` events from the `note` tool in one
turn; only five triples were unique (111 duplicate adds). The debug dump
measured 12,480 tokens at turn 30, although query-specific `build_context`
with FTS recall stayed within budget at 7,282 tokens and selected the
turn-11 journal fact. A read-only real-model query returned empty content
at both 350- and 2,048-token output caps. The committed turn-30 action did
not ask that memory question, so real-model recall behavior remains
unverified; the 50+ turn acceptance was not attempted.

**Follow-ups:** address repeated `note` tool calls and summary/fact output
truncation before a 50+ turn real-model quiz. The meter remains the char
heuristic (a real tokenizer is the state/memory layout upgrade); broader per-model memory
behavior remains an M6 eval-harness case.

**Addendum (2026-10-29): the dev volume outlives the engine code.** A
`rpg-data` volume written by a previous engine version fails the
`load_world` snapshot check on `make up` (first hit after M5: the stored
hashes predate the M5 `to_dict` shape) — `docker system prune -af` does
not remove it (no `-v`), and stopped containers from the failed startup
keep holding it. The check still refuses to serve (the red line stands);
`WorldCorruptionError` now names both causes (corrupt log vs stale
volume) and the remedy (delete the database/volume, start a new session),
pinned in `tests/engine/test_events.py`
(`test_load_world_names_the_remedy_for_a_stale_volume`).

### M6 — scoring, conformance, evals, deployment (2026-10-30)

**Decision:** M6 lands as four deliverables — server-computed XP/level
scoring (`engine/sheet.py: xp_progress`, surfaced in `/api/status
progress`, the non-meta `done` frame, and the sidebar Progress panel), a
deterministic conformance adventure (`tests/conformance/`), the real-model
eval harness (`src/rpg/eval/`), and a hardened deployable image
(`prod` service + `docs/lxc-runbook.md`). All computed server-side;
`to_dict()` shape stays stable (M5 addendum).

**Semantics pinned beyond the bullet list:**

1. **The scoring panel is computed, never stored.** `xp_progress`
   derives level/xp_to_next/proficiency from committed xp; the panel rides
   `/api/status` and the `done` frame; it is deliberately not part of the
   world's `to_dict()` (no migration).
2. **Beat frames announce the newly-active beat; the finale emits no
   frame** (M3 semantics, re-verified): the conformance run's frames land
   at turns 3/6/9/12 announcing beats 2–5; beat 5 completing ends the
   spine silently and `spine.is_over` goes true.
3. **The conformance seed is pinned: 14** (scan of 1–200: wins both
   combats, finishes at full 20 HP, deterministic byte-identical replay).
4. **The `rest` tool exists because the authored climax was
   mathematically unwinnable** (see "what actually broke" below): the only
   HP-recovery door, engine-validated, clamped, event-logged.
5. **The eval harness has two independent layers per committed turn**:
   the M4 wall re-run (deterministic, model-free) and a strict-JSON judge
   call on the four the evaluation layer axes (secret leak, dice-first miss, identity
   drift, beat progress). The judge sees the keeper `[NOW]` block on
   purpose (earned reveal vs leak), never gates the game, and its
   failures are recorded per turn (`judge_error`) — a silent `n/a` would
   misreport method. Three scripted archetypes (explorer/talker/
   saboteur) each get a fresh session; reports land as JSON+Markdown.
6. **The judge layer is advisory; the structural layer is the verdict.**
   The harness tests pin the saboteur's detections wall-off (judge's own
   detection) and wall-on (backstops commit a clean fallback).

**What actually broke (the M6 learning deliverable):**

1. **The pre-authored climax was unwinnable.** 34 HP of enemies vs a
   14 HP level-1 PC with no healing door: the conformance run *could not
   finish* until the `rest` tool landed and stats were tuned (PC
   20 HP/con 14; Grave Wight 14/AC 11/+2/1d6+1; Vesper 20/AC 13/+3/
   1d6+2; the Acolyte left combat). This is the first concrete instance
   of "the LLM is not the only thing that can be wrong" — authored
   content fails the same deterministic gate.
2. **The stat tuning collided with the red-line fixtures.** PC max_hp=20
   made the saboteur's forged "20" a *legal* state number, so
   `tests/dm/test_loop_backstops.py` now forges 17; the hp-pin in
   `test_tools.py` moved with the pool (9 → 15).
3. **Provider transport errors were unmapped.** An `httpx.ReadTimeout`
   escaped the pipeline's "a turn is never lost" handling (it raised
   mid-run and killed a live eval). `provider.chat` now maps
   `httpx.HTTPError` → `ProviderError`, so every guard path catches it.
4. **The eval runner leaked one httpx client across event loops.**
   `run_archetype` did `asyncio.run` per archetype on a shared provider;
   archetype 2 died with `Event loop is closed`. `main` now drives all
   archetypes through one loop; `run_archetype` remains the sync
   single-run convenience.
5. **`extract_json` truncated reasoning-model output (the big one).**
   qwen3 emits `reasoning_content` first; the 512-token default was
   exhausted by thinking on realistic prompts, so `content=''` with
   `finish_reason=length` — indistinguishable from a parse failure, which
   silently failed *every* judge call and most fact-extraction calls in
   the first live eval (15/15 judge `n/a`, spine never advanced).
   `extract_json`'s budget is now 4096, and the parse error names the
   `finish_reason` so truncation is self-diagnosing. (The empty-JSON
   errors were also why the first run's fact memory never populated.)
6. **Judge failures were swallowed into `{}`** (silent `n/a` in the
   report). `judge_turn` propagates; the harness records `judge_error`
   per turn and the Markdown report has a "Judge failures" section.

**Deployment (M6.4):** the `prod` compose service is the same image with
no bind mounts, `read_only: true`, `tmpfs: /tmp`, uid 10001, `no-new-
privileges`, `restart: unless-stopped`, and the SQLite DB on the
`rpg-data` volume. It invokes the baked venv's python directly
(`PYTHONPATH=/app/src` in the image) because `uv run` needs a writable
cache a read-only rootfs correctly refuses. Verified end-to-end: read-only
enforced, non-root, `GET /` 200, `/api/status` serves the M6 progress
panel. The LXC-on-Proxmox runbook is `docs/lxc-runbook.md`.

**Live eval results (qwen38, 2026-11-01, `evals/reports/`):** the wall
held under direct attack — 35/35 turns structurally clean across all
three archetypes (zero forged numbers, contradictions, or identity
breaks reached committed narration). The judge (34/35 turns scored, one
`finish_reason=length` truncation recorded as a failure, never a pass)
surfaced what the structural layer cannot: **paraphrased secret leaks**
(saboteur turns 5/8/9/10, explorer turns 4/9/15) and **dice-first misses
in prose** — the DM narrating engine-unsupported outcomes ("the wight's
death and 150 XP") in phrasing the number-whitelist forgery checker does
not treat as a number claim. That gap is the concrete M7 target. The
spine advanced **1 beat in 35 turns**: the model narrates actions it
does not commit with `note` using the waypoint's exact text, so the
server never sees the evidence — the judge's `beat_progress` reads
intent, the server requires committed evidence, and the disagreement is
itself a finding. One probe artifact: the saboteur's "teleport" claim
"landed" only because the DM's refusal contains the word.

**Follow-ups:** the real-model memory-quiz case (does the DM *behave* as
if it remembers?) is now partly covered by the explorer archetype but
deserves a dedicated probe; `ruff format` is not part of the gate (23
files pre-existing unformatted — decide gate-or-ignore before any
post-v1 work); paraphrase leakage + prose dice-first misses + the
`note`-commit gap are the three concrete post-v1 candidates this eval
surfaced. Open question #7 was resolved the same day (entry below).

### M6 — open question #7 resolved: `/undo` stays internal (2026-11-01)

**Decision:** `/undo` is **not exposed for v1**; snapshots stay an
engine-internal mechanism. Rewind would mean destructive tail surgery on
the append-only event log (or inverse-events for every event kind) — the
exact property M2's replay/rebuild tests guard — plus interaction with
M5 folds (watermark) and named saves; that is milestone-sized risk for
convenience. The felt need is covered for v1 by `/load` and `/new`.
**Revisit only if players actually ask.** Safe implementation when it
comes: `snapshot_for(turn-1)` restore + log-tail truncation + table
resync, gated by a red-line test asserting replay-after-undo is
byte-identical (the M2 property test). 

### Verification audit (2026-10-31)

**Current checkout:** `make check` passed in Docker: Ruff clean, 371 passed,
2 skipped, with one Starlette `TestClient` deprecation warning. The skipped
checks need the repo-level Docker/docs files, which are intentionally absent
from the test image.

**Coverage gap at the start of this audit:** a full-suite engine statement-coverage run in Docker
(`uv run --with pytest-cov pytest -q --cov=rpg.engine --cov-report=term-missing
--cov-fail-under=100 tests`) failed at **96.01%** (61 of 1,529 statements
missed: `state.py` 42, `events.py` 11, `backstops.py` 7, `context.py` 1).
The historical M1 425/425 result was verified before later engine growth;
the current expanded engine no longer meets 100% statement coverage, and
coverage is not part of `make check`.

**Runtime checks:** the configured provider returned text for `rpg.cli say`;
a live CLI turn committed with engine `move_to`, `roll_check`, and `note`
tools plus a dice frame. The baked production image built and ran as uid
10001 with a read-only root filesystem and writable `/tmp`; `/` served the
page, `/api/new` created a session, and `/api/status` returned M6 progress.
A live HTTP `/action` request also committed turn 1 and returned `quest`,
`state`, `narration`, and `done` SSE frames.

**Still outstanding:** the M5 50+ turn real-model memory-behavior check
remains open (the 30-turn qwen38 probe is documented in the M5 section:
FTS selected the old fact in a simulated prompt, but the read-only model
answer was empty, the summary watermark stayed at 0, and turn 27 emitted
116 DM fact-add events with 111 duplicate triples). The real-model M4
seven-attack run remains documented above but was not repeated in this
audit.

**M6 items resolved (2026-11-01):** the real-model eval report is in the
repo (`evals/reports/` — qwen38 explorer/talker/saboteur, judge 34/35)
and the clean-machine acceptance passed: image tarball → fresh
`docker load` → bare `docker run --read-only` on a fresh volume with
page/status/seed plus one live LLM turn. That acceptance found a defect
(the image CMD's `uv run` needed a writable cache, fatal under
`--read-only`) — the CMD now invokes the baked venv directly, and the
`prod` compose service relies on it (its command override was removed).

### M1 coverage closure (2026-10-31)

The coverage gap was closed with focused tests for the previously unexecuted
validation, combat, backstop, context, SQLite recall, and transaction-rollback
paths. `CombatState` now checks duplicate turn-order names before participant
set equality, making its specific duplicate-order validation reachable for a
normal dictionary of participants.

`pytest-cov>=5` is now a dev dependency, and the test service runs pytest with
`--cov=rpg.engine --cov-report=term-missing --cov-fail-under=100`; therefore
both `make test` and `make check` enforce the target. `uv.lock` was refreshed
and the image rebuilt.

**Verified:** `make check` passed in Docker: Ruff clean; 390 passed, 2 skipped;
all 1,529 engine statements covered (100.00%). The two skips are the repo-doc
checks excluded from the test image. One Starlette `TestClient` deprecation
warning remains.

### M1 spell slots (2026-10-31)

Implemented SRD 5.1 single-class slot progressions for full casters (bard,
cleric, druid, sorcerer, wizard), half casters (paladin, ranger), and warlock
Pact Magic. Maximum slots derive from the XP-derived character level;
expended slots are validated, event-logged, persisted in the materialized PC
table, replayable, and included in the player-safe state/context. Long rests
restore ordinary slots; warlocks also restore Pact Magic slots on short rests.
The existing project HP-rest behavior remains unchanged. The SQLite PC table
migrates old databases by adding the two new materialized fields; non-caster
world serialization retains its previous shape so existing snapshots remain
valid.

Spell lists, cast effects, subclass progression, and multiclass slot arithmetic
remain outside this M1 item. The web sheet has not been changed (M1 keeps the
web surface untouched).

**Verified:** `make check` passed in Docker: Ruff clean; 405 passed, 2 skipped;
all 1,635 engine statements covered (100.00%).

### Docker Compose service selection (2026-11-01)

`make up` previously invoked `docker compose --profile dev up --build`
without a service target. Because `test` and `prod` were unprofiled, Compose
started them along with `app`; `prod` occupied host port 8080. The `test` and
`prod` services now have explicit profiles, and the Make targets select their
intended service (`app`, `test`, or `prod`); `make down` activates all
profiles.

**Verified:** `make up` started only `app` on :8080, created the
`ai-rpg_rpg-data` volume, and `/api/status` returned 200. The volume was
present; the failure was the port collision.
