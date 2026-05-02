---
task: random_event
purpose: Escalate an unanswered disruption/danger event (random_events._escalate_event)
placeholders:
  old_text: Previous event text
  new_category: New category (e.g. "danger")
  language_name: Target language name
---
## system
You escalate roleplay events. Make them more urgent.

## user
An event happened but nobody reacted:
"{{ old_text }}"

The situation has escalated. Write the NEXT event — more urgent, more serious, demanding immediate action.
Category: {{ new_category }}
Write in {{ language_name }}.
Write ONE short sentence (max 120 characters).
Reply with ONLY the escalated event text.
