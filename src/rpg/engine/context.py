"""Context builder (roadmap M2): stable head + volatile tail + trimmed history.

Layout (Loreweaver's split, the state/memory layout):
- stable head (system message, cacheable): DM identity, rules summary,
  GM-quality standards, keeper-discipline block, spine [NOW] block,
  rolling campaign summary.
- volatile tail (user message, headed "Current game state
  (engine-provided, not spoken by any player)"): location, objective, PC
  sheet, NPC roster, open quests, recalled facts, trimmed transcript,
  player line.

M2 also meters the prompt (token/char heuristic) and adds the
`/dump-context` debug command.
"""
