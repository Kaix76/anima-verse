---
task: consolidation
purpose: Monthly summary — collapse a month's weekly summaries into a 5-10 sentence narrative (memory_service._consolidate_weekly_to_monthly)
placeholders:
  month_key: Month key "YYYY-MM"
  character_name: Character whose month is being summarized
  entries_text: Bullet list of "- YYYY-WNN: <weekly summary>" entries
---
## system
You are a summarization assistant. Reply with ONLY the summary — no JSON, no explanation, no commentary.

## user
Summarize the month {{ month_key }} for {{ character_name }}.

Weekly summaries:
{{ entries_text }}

Write 5-10 compact sentences from {{ character_name }}'s perspective (third person).
Focus on: major events, relationship developments, personal growth, turning points.
Reply with ONLY the summary.
