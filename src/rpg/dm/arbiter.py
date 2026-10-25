"""Arbiter (roadmap M4): what is the player's text actually?

- meta commands (/status /roll /save /load /help /ooc) -> engine, no LLM
- in-game text -> intent, never outcome; wrapped as untrusted data
  (`<player_action>`) so embedded instructions are never followed
- identity enforcement: second person only (solo play; the model may not
  start playing the player's side)
"""
