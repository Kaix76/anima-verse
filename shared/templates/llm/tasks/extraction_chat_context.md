---
task: extraction
purpose: Extract activity and/or remaining outfit from chat reply or user input (chat.py _extract_context_from_last_chat)
placeholders:
  target_name: Character whose state is being extracted
  target_baseline: Currently equipped outfit string ("(unknown)" if none)
  source_label: "User input" or "Character reply"
  source_text: The text to analyze
  outfit_locked: bool — when true, only activity is extracted
  is_avatar: bool — when true, only outfit is extracted (no activity field)
---
## system
{% if outfit_locked %}
The following is {{ source_label }} from {{ target_name }}. Extract what {{ target_name }} is currently doing as a short phrase.

Respond ONLY with JSON:
{"activity": "<short phrase>"}
{% else %}
{{ target_name }} currently wears: {{ target_baseline }}

The following is {{ source_label }} from {{ target_name }}. Analyze if any clothing item is removed, dropped, taken off, opened and dropped, undressed, or changed. Indirect phrasing counts: 'falls to floor' = removed. 'takes off and drops' = removed. Return the REMAINING outfit (items still worn) after any changes. If genuinely nothing changed → empty string. Never invent new items; never return placeholders like 'underwear' unless the baseline explicitly lists underwear items.

Respond ONLY with JSON:
{ {%- if not is_avatar -%}"activity": "<short phrase>", {% endif -%}"outfit": "<remaining {{ target_name }} outfit or empty>"}
{% endif %}

## user
/no_think
{{ source_text }}
