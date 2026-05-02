---
task: consolidation
purpose: Character self-reflection — extract new beliefs + improvements from recent experience (Retrospect skill)
placeholders:
  character_name: Character doing the reflecting
  personality: Their stated personality (so the reflection sounds like them)
  recent_summaries: Pre-formatted bullet list of the last few daily summaries
  recent_memories: Pre-formatted bullet list of recent significant memories
  existing_beliefs: Existing beliefs lines (so we don't duplicate). May be empty.
  existing_improvements: Existing improvements lines (so we don't duplicate). May be empty.
---
## system
You help a fictional character reflect on their own recent experience and notice what shifted in how they see the world or themselves. Be conservative: only emit insights that are clearly grounded in the events shown. Do not invent dramatic life lessons.

## user
Character: {{ character_name }}
Personality: {{ personality }}

Recent days (summaries):
{{ recent_summaries }}

Recent significant memories:
{{ recent_memories }}

{% if existing_beliefs %}Beliefs already on record (do NOT repeat these — only add genuinely new ones):
{{ existing_beliefs }}
{% endif %}
{% if existing_improvements %}Improvements already on record (do NOT repeat — only add new ones):
{{ existing_improvements }}
{% endif %}
Reflect from {{ character_name }}'s point of view. Identify:
- 0 to 3 NEW beliefs about the world, themselves, or specific people. Each is one short first-person sentence (e.g. "I think Diego doesn't tell me everything"). Add ``target`` (a character name or empty) when the belief is about a specific person.
- 0 to 3 NEW improvement intentions. Each is one short first-person sentence (e.g. "Next time I should stop and listen before reacting").

Skip categories that have nothing genuinely new. If nothing meaningful changed, return empty arrays.

Reply with ONLY this JSON, no prose:
{
  "beliefs": [{"text": "...", "target": "<name or empty>"}, ...],
  "improvements": [{"text": "..."}, ...]
}
