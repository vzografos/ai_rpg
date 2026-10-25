"""All prompt templates in one module (the documented layout: llm/prompts.py).

M0 carries only what the skeleton needs (ping + a minimal "turn"). M3 grows
this into the full layout: DM identity + rules summary + GM-quality standards
+ keeper-discipline block + story spine [NOW] block + rolling summary in the
cacheable *stable head*; engine-provided state header + trimmed transcript +
player line in the *volatile tail* (Loreweaver's stable/volatile split).
"""

# --- M0: connectivity ping ---------------------------------------------------

PING_SYSTEM = (
    "You are the Dungeon Master of a text-only D&D 5e game. "
    "Reply in at most one short sentence."
)

PING_USER = "Say hello to the player and confirm you are ready to run the game."

# --- M0: the skeleton's only "turn" ------------------------------------------
# M3 replaces this with the full turn pipeline (dm/loop.py): intent decision,
# DM round loop with tools, engine adjudication, backstops. Until then the web
# UI proves the POST /action -> SSE path end-to-end with a one-shot completion.

ACTION_SYSTEM = (
    "You are the Dungeon Master of a text-only D&D 5e game. "
    "The player has just described what their character does. "
    "Narrate what happens in 2-4 short sentences, always in second person "
    "('you'), and end by asking what they do next. "
    "Do not invent dice results; no rolls are provided yet."
)


def action_messages(player_text: str) -> list[dict[str, str]]:
    """M0 one-shot turn: system + the raw player line.

    M4 wraps player text as untrusted data (`<player_action>`) and M3 feeds
    real state — this helper is where that happens.
    """
    return [
        {"role": "system", "content": ACTION_SYSTEM},
        {"role": "user", "content": player_text},
    ]
