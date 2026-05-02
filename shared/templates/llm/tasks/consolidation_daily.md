---
task: consolidation
purpose: Daily summary — collapse one day's episodic memories into a 3-5 sentence narrative (memory_service._consolidate_episodics_to_daily)
placeholders:
  day_str: Date string "YYYY-MM-DD"
  character_name: Character whose day is being summarized
  existing: Existing daily summary text (empty if none)
  contents: Bullet list of that day's episodic memories
---
## system
You are a summarization assistant. Reply with ONLY the summary — no JSON, no explanation, no commentary.

## user
Summarize the day {{ day_str }} for {{ character_name }}.

{% if existing %}
Existing daily summary:
{{ existing }}
{% endif %}
Individual memories from this day:
{{ contents }}

Write 3-5 compact sentences from {{ character_name }}'s perspective (third person).
Focus on: key moments, people involved, emotions, decisions.
Reply with ONLY the summary.
