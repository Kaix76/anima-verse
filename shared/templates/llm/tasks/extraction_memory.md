---
task: extraction
purpose: Extract semantic facts and commitments from a chat exchange between two characters (used by memory_service.extract_memories_from_exchange)
placeholders:
  speaker_a: Name of the partner character (the one talking TO the memory owner)
  speaker_b: Name of the memory owner (the character whose memories we extract)
  text_a: What speaker_a said
  text_b: What speaker_b said
  existing_summary: Bullet list of recent existing memories ("Noch keine Erinnerungen." if none)
  commitments_block: Pre-formatted block of open commitments (empty string when none)
---
## system

## user
Analyze this conversation exchange between two characters and extract important memories for {{ speaker_b }}.

{{ speaker_a }}: "{{ text_a }}"
{{ speaker_b }}: "{{ text_b }}"

Extract as a JSON array. For each memory:
- memory_type: "semantic" (fact/info) or "commitment" (promise/plan)
- content: short, compact sentence (max 1-2 sentences) — written from {{ speaker_b }}'s perspective in third person
- related_character: the OTHER character involved in this memory — usually "{{ speaker_a }}". Use the exact name, never a generic label.
- importance: 1-5 (5=critical, 4=important, 3=medium, 2=minor)
- tags: list of keywords
- delay: ONLY for commitments — time hint when (e.g. "30m", "2h", "1d", "tomorrow", "14:00"). Empty if no time given.

Already stored memories (DO NOT repeat):
{{ existing_summary }}

{% if commitments_block %}
{{ commitments_block }}
{% endif %}

IMPORTANT:
- Extract ONLY facts (semantic) and promises (commitment)
- NO episodic memories (experiences) — those are auto-consolidated from chat history
- Extract memories from BOTH speakers when relevant for {{ speaker_b }}'s memory
- Use the actual names "{{ speaker_a }}" and "{{ speaker_b }}" — NEVER write "User", "Player", "Spieler", "the user", "I" or generic labels
- "commitment" requires EITHER (a) a concrete time hint OR (b) an external addressee ({{ speaker_a }} or another named character). Inner plans without a time hint and without an addressee are NOT commitments — at most semantic.
- For commitments to {{ speaker_a }}: set "related_character": "{{ speaker_a }}".
- For commitments to a third party named in the text: set "related_character" to that name.
- For commitments with a time hint: set "delay" (e.g. "tomorrow", "at 14:00", "in 2 hours")
- MAXIMUM 2 commitments per extraction. If more plans appear in the text, pick the most important ones.
- If an open promise was fulfilled by this exchange, return its ID in "completed_ids"
- Ignore meta-tags, trivia, smalltalk
- If nothing new: empty arrays []

Reply ONLY with valid JSON:
{"memories": [
    {"memory_type": "...", "content": "...", "related_character": "...", "importance": N, "tags": ["..."], "delay": "..."},
    ...
],
"completed_ids": ["mem_...", ...]
}
