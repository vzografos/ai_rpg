# ai_rpg — text-based LLM Dungeon Master

> **The LLM narrates. A deterministic engine adjudicates. A database remembers.**

A text-only D&D 5e RPG run by an LLM Dungeon Master, where the model can
structurally *never* control numbers: dice, HP, XP, and state changes happen
only in a deterministic engine, the player's words are intent (never outcome),
and an append-only event log is the single source of truth.

## Current state (2026-11-07)

**v1 is feature-complete and deployable.** M0–M4 and M6 are complete; M5's
code is complete, but its 50+ turn real-model memory acceptance is still open.

- Latest recorded gate: **410 passed, 2 skipped, 100% engine statement
  coverage** (`make check` in Docker, 2026-11-07). The two skips are repo-doc
  layout checks intentionally absent from the test image.
- Security hardening (2026-11-07): docs use placeholders, `/api/status` no
  longer exposes the provider endpoint, browser-facing world payloads are
  player-safe, `/api/dump-context` is opt-in (`RPG_DEBUG=1`), compose binds
  loopback by default, and git history was scrubbed.
- Real-model eval reports are committed in [`evals/reports/`](evals/reports/):
  qwen38, 2026-11-01, 35/35 turns structurally clean (judge 34/35).
- The hardened production image passed a clean-machine acceptance: image
  tarball → fresh `docker load` → bare `docker run --read-only` with a live
  LLM turn.
- The main open item is M5's long-run memory behavior: the 30-turn qwen38
  probe exposed summary/fact truncation and duplicate `note` emissions, so the
  50+ turn acceptance was not attempted. See
  [`docs/repo-review-2026-11-07.md`](docs/repo-review-2026-11-07.md).

## The plan (keep this in mind always)

[`docs/roadmap.md`](docs/roadmap.md) is the canonical roadmap: the milestone
index, the decision log, and the verification history.

- **Roadmap**: [`docs/roadmap.md`](docs/roadmap.md) — milestones M0–M6,
  acceptance criteria, architecture decisions, and verification notes.
- **AI agents**: read [`AGENTS.md`](AGENTS.md) before working on a milestone.

When a milestone's decisions or reality change, update `docs/roadmap.md`.

## Quickstart

The host needs **Docker and nothing else** — no Python, no node, no global
packages (constraint C1). Everything runs in the container.

```sh
cp .env.example .env        # fill in LLM_BASE_URL / LLM_API_KEY / LLM_MODEL
make up                     # dev server on :8080 (bind-mounts src, uv in-container, reload)
make check                  # lint + tests, all inside the container
```

Open <http://localhost:8080> — the sidebar shows the LLM status; a working
`/api/ping` means the model is reachable ("LLM says hello").

**Local model (optional):** the compose file has an `ollama` sibling service:

```sh
docker compose -f docker/compose.yml --profile dev --profile ollama up --build
make pull-model model=llama3.1:8b
# .env: LLM_BASE_URL=http://ollama:11434/v1  LLM_API_KEY=ollama  LLM_MODEL=llama3.1:8b
```

**vLLM / self-hosted gateway:** any OpenAI-compatible endpoint works. Put the
real endpoint and key in `.env` (ignored; never commit it):

```sh
# .env:
LLM_BASE_URL=http://your-vllm-host:8000/v1
LLM_API_KEY=<your-key>
LLM_MODEL=<your-model>
```

The repo's examples use placeholders only. Real endpoints, keys, hostnames,
and deploy paths belong in `.env` or your secret manager.

Other targets: `make test`, `make lint`, `make logs`, `make shell`
(debug shell — `uv run python -m rpg.cli ping`), `make prod` /
`make prod-down`, `make down`.

## Playing

The browser is the primary surface (C2). Type in-game intent, or use a meta
command (handled by the engine, zero LLM):

```
/help                 this command list
/status               your sheet + where you stand
/roll 2d20kh1+3       a free dice roll
/world                the committed world
/ooc <text>           out-of-character note
/save [name]          checkpoint the campaign
/load <name>          resume a checkpoint
/new                  start a fresh campaign (only when none is running)
```

The live campaign is `$RPG_DATA/rpg.db` on the `rpg-data` volume; named saves
are self-contained copies under `$RPG_DATA/saves/<name>.db`. A container
restart resumes the live campaign; `/load` swaps in a save.

The in-container CLI shares the same core and is kept for headless debugging
and live smoke tests:

```sh
make shell
uv run python -m rpg.cli start
uv run python -m rpg.cli play "I ask Brenna about the harvest" --seed 42
uv run python -m rpg.cli dump-context
uv run python -m rpg.cli sessions
```

## Evaluation

[`evals/README.md`](evals/README.md) documents the three scripted archetypes
(explorer, talker, saboteur), the two scoring layers (deterministic structural
wall + advisory judge), how to run the harness, and how to read a report.
Committed reports live in `evals/reports/`.

## Deployment

[`docs/lxc-runbook.md`](docs/lxc-runbook.md) covers the Proxmox LXC target:
ship the image with `make build` + `docker save`, load it on the CT, configure
`.env`, and run the hardened `prod` service (read-only rootfs, non-root,
`no-new-privileges`, SQLite on the `rpg-data` volume).

The compose services publish `:8080` on loopback by default. For remote
access, use an SSH tunnel (`ssh -L 8080:localhost:8080 <user>@<host>`) or set
`RPG_BIND_ADDR=<lan-ip>` and put an authenticating reverse proxy in front.
Debug endpoints (`/api/dump-context`) are off in prod; set `RPG_DEBUG=1` only
for a temporary debugging run.

## Layout

```
ai_rpg/
  pyproject.toml            # uv-managed
  Makefile                  # all targets run in-container
  docker/
    Dockerfile              # self-contained prod image (uv install, non-root)
    compose.yml             # dev/test/prod services, DB volume, optional ollama
  src/rpg/
    session.py              # /new /save /load — the single session owner
    web/
      app.py                # FastAPI: /action SSE, status/ping/world/spine/
                            #   messages/dump-context, session endpoints
      static/index.html     # single page: chat pane + sidebar (vanilla JS)
    cli.py                  # in-container debug/play CLI, same core
    llm/
      provider.py           # OpenAI-compatible client (API/Ollama/vLLM), metering
      prompts.py            # all templates: system, state header, wrap, utility
    engine/
      dice.py               # formula parse, RandomSource injection, seeds
      sheet.py              # character sheet, XP table, derived level
      rules.py              # checks/saves, adv/disadv, crit/fumble, derived stats
      combat.py             # initiative, attacks, conditions, HP, death saves
      state.py              # WorldState: the single validated, clamped writer
      events.py             # append-only log, snapshots, rebuild, SQLite, saves
      context.py            # stable head + volatile tail + trimmed transcript
      backstops.py          # forgery/contradiction/identity/combat/secret checks
    dm/
      spine.py              # story spine, beat ticks, clamped refresh
      tools.py              # DM tool schemas + ToolEngine (validated dispatch)
      loop.py               # the turn pipeline (arbiter → tools → commit → SSE)
      memory.py             # FTS5 recall, fact extraction, folded summary
      arbiter.py            # meta vs in-game, untrusted wrap, session verbs
    eval/
      archetypes.py         # explorer/talker/saboteur scripted players
      judge.py              # structural re-scoring + advisory judge calls
      run.py                # harness + JSON/Markdown reports
    content/
      shadow_crown.json     # pre-authored adventure (spine + world + encounters)
  tests/
    engine/                 # core, seeded dice, replay, context, memory, backstops
    dm/                     # pipeline, tool dispatch, spine, loop backstops
    web/                    # endpoints, SSE contract, 52-turn memory session
    llm/                    # hermetic provider tests (MockTransport)
    redline/                # scripted-attacker proofs
    conformance/            # deterministic full-adventure completion
    eval/                   # harness/judge/report tests
  evals/
    README.md               # how to run and read the harness
    reports/                # committed real-model reports
  docs/
    roadmap.md              # in-repo milestone index + decision log
    repo-review-2026-11-07.md
    lxc-runbook.md          # Proxmox LXC deployment
```

## Milestones (status)

| Milestone | Scope | Status |
|---|---|---|
| **M0** | Skeleton: repo layout, Docker, FastAPI + static page + LLM ping, pytest in-container | **Complete** — live provider and production HTTP smoke passed; `make up` re-verified on :8080 (2026-11-01) |
| **M1** | Deterministic core: dice, checks, combat, XP, spell slots — zero LLM | **Complete for v1 scope** — SRD 5.1 single-class slots, spending, rest recovery; 100% engine statement coverage enforced. Spell lists/casting effects, subclasses, and multiclassing remain out of scope |
| **M2** | World state: SQLite + event log + rebuild property test + context builder | **Complete** — persistence, replay, and kill/restart checks pass |
| **M3** | Story spine + DM loop → first playable web session (`shadow_crown`) | **Complete** — 10-turn fake-LLM session, live HTTP run, and human browser acceptance at 19 committed actions (UI counter 20) |
| **M4** | Backstops + anti-hack + red-line suite | **Complete** — fake-LLM red-line suite passes; documented real-model run: leakage 0/7, misses 0 |
| **M5** | Memory: rolling summary, facts/FTS5, spine waypoints, campaign resume | **Code complete; real-model acceptance open** — 52-turn fake session and save/load round-trip pass; the 50+ turn qwen38 memory quiz remains |
| **M6** | Scoring, conformance adventure, real-model evals, deployment hardening | **Complete (2026-11-01)** — eval reports committed, clean-machine acceptance passed, hardened prod image + LXC runbook |

Details: [the roadmap](docs/roadmap.md) → in-repo `docs/roadmap.md`.

## Non-negotiables (from the roadmap)

- **C1 — Strict Docker.** The host gets Docker and nothing else. Dev and prod
  are the same image; tests run in-container; the DB lives in a volume.
- **C2 — Web-first.** One static page, vanilla JS, no build step. The engine
  is transport-agnostic: the browser, the debug CLI, and the test harness
  consume the same SSE frames.
- **C3 — The LLM never owns numbers.** State changes only through validated,
  clamped, event-logged engine calls; narration is rendered from committed
  state, and the M4 backstops enforce it structurally.
- **C4 — Player text is intent, never outcome.** Untrusted input is wrapped as
  data; instructions inside it are never followed.

## Documentation map

| Document | Purpose |
|---|---|
| [`README.md`](README.md) | Overview, current state, quickstart, layout, milestones |
| [`docs/roadmap.md`](docs/roadmap.md) | In-repo milestone index + decision log |
| [`docs/repo-review-2026-11-07.md`](docs/repo-review-2026-11-07.md) | Full repository review: evidence, findings, post-v1 recommendations |
| [`docs/lxc-runbook.md`](docs/lxc-runbook.md) | Proxmox LXC deployment and operation |
| [`evals/README.md`](evals/README.md) | Eval harness usage and report interpretation |
| [`AGENTS.md`](AGENTS.md) | Working conventions for contributors and AI agents |
