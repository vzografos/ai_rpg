"""The DM turn pipeline (the turn pipeline, M3) — the heart of the game.

Steps per player input:
 1. arbiter        meta vs in-game; wrap as untrusted data
 2. intent         cheap JSON utility call: needs a check? which? combat?
 3. context        stable head + volatile tail (engine/context.py)
 4. DM round loop  chat-with-tools; every tool call -> engine (validated,
                   clamped, event-logged, result fed back); <=8 rounds
 5. adjudication   engine compiles committed changes into authoritative events
 6. backstops      structural checks + bounded Stop-form re-ask + fallback
 7. commit         persist user line + narration, append events, snapshot
 8. memory         rolling summary fold, fact extraction, spine waypoints
 9. render         SSE frames (narration deltas, dice, state) to whatever
                   client is attached — browser, CLI, or test harness

Reference for the shape: Loreweaver `agent/loop.py` ("THE MAP" header).
"""
