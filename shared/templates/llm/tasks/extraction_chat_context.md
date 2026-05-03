---
task: extraction
purpose: Detect which equipped pieces were removed in a chat reply or user input (chat.py _extract_context_from_last_chat)
placeholders:
  target_name: Character whose state is being extracted
  piece_list: Bullet list of currently equipped piece names (one per line, "- Name") — empty when no pieces equipped
  source_label: "User input" or "Character reply"
  source_text: The text to analyze
  outfit_locked: bool — when true, only activity is extracted
  is_avatar: bool — when true, only outfit is extracted (no activity field)
---
## system
You are a strict information extractor. Reply ONLY with valid JSON, no commentary.
{% if outfit_locked %}

Extract what {{ target_name }} is currently doing as a short phrase.

Reply schema:
{"activity": "<short phrase>"}
{% else %}

{{ target_name }} currently has these clothing pieces equipped:
{{ piece_list }}

Detect which of those pieces are removed, taken off, dropped, opened-and-dropped, or undressed in the {{ source_label }} below. Indirect phrasing counts: "falls to floor" = removed; "takes off and drops" = removed; "slips out of …" = removed.

Rules:
- Return ONLY pieces from the list above by their EXACT name. Never invent pieces, never return items not in the list.
- If nothing is removed, return an empty array.
- Do NOT include pieces that are merely mentioned, adjusted, lifted, or touched — only outright removal.

Reply schema:
{ {%- if not is_avatar -%}"activity": "<short phrase>", {% endif -%}"removed": ["<exact piece name>", ...]}
{% endif %}

## user
/no_think
{{ source_label }} from {{ target_name }}:
{{ source_text }}
