"""ai_rpg test suite.

Layout mirrors the engine/DM split (the documented layout):
- tests/llm      provider client, hermetic (httpx.MockTransport)
- tests/web      FastAPI endpoints + SSE frame contract
- tests/engine   M1+: pure unit tests, seeded dice
- tests/loop     M3+: fake-LLM integration tests of the turn pipeline
- tests/redline  M4+: scripted-attacker + secret-leak checks
"""
