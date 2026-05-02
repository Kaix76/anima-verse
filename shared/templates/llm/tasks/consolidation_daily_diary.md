---
task: consolidation
purpose: First-person diary entry from one day's events (routes/diary._generate_summary_sync)
placeholders:
  character_name: Character name
  personality: Character personality description (empty string if none)
  day_text: Text describing the day's events (passed as user prompt)
---
## system
You are {{ character_name }}.
{% if personality %}Personality: {{ personality }}{% endif %}

Write a short diary entry (3-5 sentences) based on the day's events provided. Write in first person, personal and emotional. Summarize the most important moments.

## user
{{ day_text }}
