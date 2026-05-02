---
task: consolidation
purpose: Daily roleplay summary (history_manager._create_daily_summary)
placeholders:
  user_display_name: User display name
  character_name: Character name (or "the character" if missing)
  context_line: Optional context line or empty
  lang_instruction: Optional "\nWrite the summary in <Language>." or empty
  history_text: Today's transcript
---
## system

## user
{{ context_line }}Summarize what happened TODAY in this roleplay conversation in 5-8 sentences.
Focus on:
- Key events and what happened (not just topics)
- Emotional moments and reactions of {{ character_name }} and {{ user_display_name }}
- Decisions made and their outcomes
- Where {{ user_display_name }} and {{ character_name }} went and what they did

Use the actual names ({{ user_display_name }}, {{ character_name }}) in the summary, not generic labels like "User" or "Assistant".
Write as a narrative summary in past tense.
Do NOT include any tool calls, commands, image URLs or code.{{ lang_instruction }}

Today's conversation:
{{ history_text }}

Summary:
