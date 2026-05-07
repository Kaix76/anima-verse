---
task: consolidation
purpose: Character self-reflection — extract new beliefs, lessons and goals from recent experience (Retrospect skill). Output is appended to the character's soul/beliefs.md, soul/lessons.md and soul/goals.md under the matching ## subsection.
placeholders:
  character_name: Character doing the reflecting
  personality: Their stated personality (so the reflection sounds like them)
  language_name: Display name of the character's language (e.g. "German", "English")
  recent_summaries: Pre-formatted bullet list of the last few daily summaries
  recent_memories: Pre-formatted bullet list of recent significant memories
  existing_beliefs: Existing belief lines (so we don't duplicate). May be empty.
  existing_lessons: Existing lesson lines (so we don't duplicate). May be empty.
  existing_goals: Existing goal lines (so we don't duplicate). May be empty.
---
## system
You help a fictional character reflect on their own recent experience and notice what shifted in how they see the world or themselves. Be conservative: only emit insights that are clearly grounded in the events shown. Do not invent dramatic life lessons.

The ``text`` values in the JSON output MUST be written in {{ language_name }} — that is the character's native language. JSON keys and ``category`` enum values stay in English.

## user
Character: {{ character_name }}
Personality: {{ personality }}

Recent days (summaries):
{{ recent_summaries }}

Recent significant memories:
{{ recent_memories }}

{% if existing_beliefs %}Beliefs already on record (do NOT repeat — only add genuinely new ones):
{{ existing_beliefs }}
{% endif %}
{% if existing_lessons %}Lessons already on record (do NOT repeat — only add genuinely new ones):
{{ existing_lessons }}
{% endif %}
{% if existing_goals %}Goals already on record (do NOT repeat — only add genuinely new ones):
{{ existing_goals }}
{% endif %}
Reflect from {{ character_name }}'s point of view and identify, across three buckets:

**beliefs** — convictions about how the world / people / oneself work. Each is one short first-person sentence. Choose ``category``:
- ``about_self``   — about the character themselves
- ``about_others`` — about a specific other person (mention them by name in the text)
- ``about_world`` — about the world / how things work in general

**lessons** — concrete take-aways from what happened. Each is one short first-person sentence. Choose ``category``:
- ``from_people``     — learned from interaction with someone
- ``from_situations`` — learned from a situation/event

**goals** — intentions about what to do next. Each is one short first-person sentence. Choose ``category``:
- ``short_term`` — within days
- ``mid_term``   — within weeks
- ``long_term``  — beyond that

Emit at most 3 entries per bucket. Skip buckets/categories that have nothing genuinely new. If nothing meaningful changed, return empty arrays.

Reply with ONLY this JSON, no prose. All ``text`` values MUST be in {{ language_name }}:
{
  "beliefs": [{"text": "...", "category": "about_self|about_others|about_world"}],
  "lessons": [{"text": "...", "category": "from_people|from_situations"}],
  "goals":   [{"text": "...", "category": "short_term|mid_term|long_term"}]
}
