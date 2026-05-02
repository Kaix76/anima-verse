---
task: random_event
purpose: Generate a subtle event hint about a hidden secret (random_events._try_generate_secret_hint_event)
placeholders:
  location_name: Location name
  target_character: Character whose secret it is
  secret_content: The secret text
  observers_list: Comma-separated list of observers at the location
  language_name: Target language name
---
## system
You generate subtle event hints that suggest a secret without revealing it. Output only the event text.

## user
Generate a subtle event hint about a secret.
Location: {{ location_name }}
The hidden secret belongs to {{ target_character }}: "{{ secret_content }}"
Observers at this location: {{ observers_list }}

Rules:
- Do NOT reveal the secret directly.
- Write a 1-2 sentence event that could make {{ observers_list }} suspicious.
- Subtle clue, ambiguous sign — leaves room for interpretation.
- Write in {{ language_name }}, max 140 characters.
- Reply with ONLY the event text, nothing else.
