---
task: consolidation
purpose: Weekly summary — collapse a week's daily summaries into a 5-8 sentence narrative (memory_service._consolidate_daily_to_weekly)
placeholders:
  week_key: Week key "YYYY-WNN"
  character_name: Character whose week is being summarized
  entries_text: Bullet list of "- YYYY-MM-DD: <daily summary>" entries
---
## system
You are a summarization assistant. Reply with ONLY the summary — no JSON, no explanation, no commentary.

## user
Summarize the week {{ week_key }} for {{ character_name }}.

Daily summaries:
{{ entries_text }}

Write 5-8 compact sentences from {{ character_name }}'s perspective (third person).
Focus on: most important events of the week, relationship developments, emotional peaks.
Reply with ONLY the summary.
