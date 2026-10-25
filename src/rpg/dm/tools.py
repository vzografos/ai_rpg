"""DM tool schemas (roadmap M3): the only door by which the LLM touches state.

Planned tools (each dispatched to the engine, validated, clamped, logged):
    roll_check, spawn_npc, move_to, start_combat, end_combat,
    grant_item, take_item, quest_update, npc_note, note, ask_clarification

The engine is the only writer of numbers; a tool call that fails validation
is rejected and the failure fed back to the model as the tool result.
"""
