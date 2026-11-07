"""ai_rpg test suite.

Layout mirrors the engine/DM split (the documented layout):
- tests/llm          provider client, hermetic (httpx.MockTransport)
- tests/web          FastAPI endpoints + SSE frame contract
- tests/engine       M1+: deterministic core, seeded dice, replay, memory
- tests/dm           M3+: turn pipeline, tool dispatch, spine, backstops
- tests/redline      M4+: scripted-attacker + secret-leak checks
- tests/conformance  M6: deterministic full-adventure completion
- tests/eval         M6: harness, judge, and report tests
"""
