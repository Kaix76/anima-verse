---
task: extraction
purpose: Extract semantic facts and commitments from a chat exchange (used by memory_service.extract_memories_from_exchange)
placeholders:
  user_display: Display name of the user (active character or fallback)
  user_message: Last user message
  character_name: Name of the responding character
  character_response: Cleaned character response (meta-tags stripped)
  existing_summary: Bullet list of recent existing memories ("Noch keine Erinnerungen." if none)
  commitments_block: Pre-formatted block of open commitments (empty string when none)
---
## system

## user
Analyze this conversation exchange and extract important memories.

{{ user_display }} (User): "{{ user_message }}"
{{ character_name }} (Character): "{{ character_response }}"

Extract as a JSON array. For each memory:
- memory_type: "semantic" (fact/info) or "commitment" (promise/plan)
- content: short, compact sentence (max 1-2 sentences)
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
- Extract from BOTH sides (user AND character)
- Use "{{ user_display }}" instead of "User" or "I"
- "commitment" requires EITHER (a) a concrete time hint OR (b) an external addressee ("promises X", "tells Y", "agrees with Z"). Inner plans without a time hint and without an addressee are NOT commitments — at most semantic.
- For commitments with a time hint: set "delay" (e.g. "tomorrow", "at 14:00", "in 2 hours")
- MAXIMUM 2 commitments per extraction. If more plans appear in the text, pick the most important ones.
- If an open promise was fulfilled by this exchange, return its ID in "completed_ids"
- Ignore meta-tags, trivia, smalltalk
- If nothing new: empty arrays []

Reply ONLY with valid JSON:
{"memories": [
    {"memory_type": "...", "content": "...", "importance": N, "tags": ["..."], "delay": "..."},
    ...
],
"completed_ids": ["mem_...", ...]
}
