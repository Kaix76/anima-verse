---
task: consolidation
purpose: Daily roleplay summary between two characters (history_manager._create_daily_summary)
placeholders:
  speaker_a: Conversation partner name (the character who talked TO speaker_b today)
  speaker_b: Memory owner name (the character whose daily summary is being written)
  lang_instruction: Optional "\nWrite the summary in <Language>." or empty
  history_text: Today's transcript (lines prefixed with the actual speaker name)
---
## system

## user
Summarize what happened TODAY in this conversation between {{ speaker_a }} and {{ speaker_b }} in 5-8 sentences.
Focus on:
- Key events and what happened (not just topics)
- Emotional moments and reactions of {{ speaker_a }} and {{ speaker_b }}
- Decisions made and their outcomes
- Where they went and what they did

Use the actual names ({{ speaker_a }}, {{ speaker_b }}) — NEVER write "User", "Player", "Spieler", "the user" or "Assistant".
Write as a narrative summary in past tense, from {{ speaker_b }}'s perspective.
Do NOT include any tool calls, commands, image URLs or code.{{ lang_instruction }}

Today's conversation between {{ speaker_a }} and {{ speaker_b }}:
{{ history_text }}

Summary:
