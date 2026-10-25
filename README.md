# ai_rpg — text-based LLM Dungeon Master

> **The LLM narrates. A deterministic engine adjudicates. A database remembers.**

A text-only D&D 5e RPG run by an LLM Dungeon Master, where the model can
structurally *never* control numbers: dice, HP, XP, and state changes happen
only in a deterministic engine, the player's words are intent (never outcome),
and an append-only event log is the single source of truth.

## The plan (keep this in mind always)

[`docs/roadmap.md`](docs/roadmap.md) is the canonical roadmap: the milestone
index, the decision log, and the verification history.

- **Roadmap**: [`docs/roadmap.md`](docs/roadmap.md) — milestones M0–M6,
  acceptance criteria, architecture decisions, and verification notes.
- **AI agents**: read [`AGENTS.md`](AGENTS.md) before working on a milestone.

When a milestone's decisions or reality change, update `docs/roadmap.md`.

## Quickstart (M0)

The host needs **Docker and nothing else** — no Python, no node, no global
packages (constraint C1). Everything runs in the container.

```sh
cp .env.example .env        # fill in LLM_API_KEY (or use the local Ollama path below)
make up                     # dev server on :8080 (bind-mounts src, uv in-container, reload)
make check                  # lint + tests, all inside the container
```

Open <http://localhost:8080> — the sidebar shows the LLM status; a working
`/api/ping` means the model is reachable ("LLM says hello").

**Local model (optional):** the compose file has an `ollama` sibling service:

```sh
docker compose -f docker/compose.yml --profile dev --profile ollama up --build
docker compose -f docker/compose.yml --profile ollama exec ollama ollama pull llama3.1:8b
# .env: LLM_BASE_URL=http://ollama:11434/v1  LLM_API_KEY=ollama  LLM_MODEL=llama3.1:8b
```

**vLLM / self-hosted gateway:** any OpenAI-compatible endpoint works. The M0
pipeline is verified end-to-end against the local vLLM server:

```sh
# .env:
LLM_BASE_URL=http://your-vllm-host:8080/v1
LLM_API_KEY=<your-key>
LLM_MODEL=qwen38
```

Other targets: `make test`, `make lint`, `make logs`, `make shell`
(debug shell — `uv run python -m rpg.cli ping`), `make down`.

## Layout

```
ai_rpg/
  pyproject.toml            # uv-managed
  docker/
    Dockerfile              # self-contained prod image (uv install, non-root)
    compose.yml             # dev: source bind-mount, uv in-container, DB volume,
                            #      optional `ollama` service; tests run in-container
  src/rpg/
    web/
      app.py                # FastAPI: POST /action, SSE stream, GET /api/status, /api/ping
      static/               # single page: chat pane + sidebar (vanilla JS, no build step)
    cli.py                  # in-container debug CLI (docker exec), same core
    llm/
      provider.py           # OpenAI-compatible client (API or Ollama/vLLM), usage metering
      prompts.py            # all templates: system, state header, player wrap, utility calls
    engine/
      dice.py               # M1: formula parse, RandomSource injection, seed support
      rules.py              # M1: checks (d20 vs DC, adv/disadv, crit/fumble), derived stats
      combat.py             # M1: initiative, attack resolution, conditions, HP
      state.py              # M2: WorldState: entities (pc, npc, item, quest, location, fact)
      events.py             # M2: append-only event log, per-turn snapshots, rebuild-from-events
      context.py            # M2: context builder: stable head + volatile tail, trimmed history
      backstops.py          # M4: forgery/contradiction/identity/combat-transition + fallback
    dm/
      loop.py               # M3: the 9-step turn pipeline
      tools.py              # M3: DM tool schemas (roll_check, spawn_npc, move_to, ...)
      arbiter.py            # M4: player input: meta vs in-game, untrusted wrap, identity
      spine.py              # M3: story spine: generation, beat tracking, clamped refresh
    content/
      shadow_crown.json     # M3: pre-authored adventure (spine + locations + NPCs + encounters)
  tests/
    llm/                    # hermetic provider tests (MockTransport)
    web/                    # endpoint + SSE frame-contract tests (fake provider)
    engine/                 # M1: pure unit tests, seeded dice
    loop/                   # M3: fake-LLM integration tests of the turn pipeline
    redline/                # M4: scripted-attacker + secret-leak checks
```

## Milestones (status)

| Milestone | Scope | Status |
|---|---|---|
| **M0** | Skeleton: repo layout, Docker, FastAPI + static page + working LLM ping, pytest in-container | **this repo today** |
| M1 | Deterministic core: dice, checks, combat, XP — zero LLM | next |
| M2 | World state: SQLite + event log + rebuild property test + context builder | |
| M3 | Story spine + DM loop → first playable web session (shadow_crown) | |
| M4 | Backstops + anti-hack + red-line suite | |
| M5 | Memory: rolling summary, facts/FTS5, spine waypoints, campaign resume | |
| M6 | Scoring, conformance adventure, real-model evals, deployment hardening | |

Details: [the roadmap](docs/roadmap.md) → in-repo `docs/roadmap.md`.

## Non-negotiables (from the roadmap)

- **C1 — Strict Docker.** The host ever gets Docker and nothing else. Dev and
  prod are the same image; tests run in-container; the DB lives in a volume.
- **C2 — Web-first.** One static page, vanilla JS, no build step. The engine
  is transport-agnostic: the browser, the debug CLI, and the test harness
  consume the same SSE frames.
