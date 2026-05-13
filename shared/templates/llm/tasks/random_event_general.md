---
task: random_event
purpose: Generate a random atmospheric event for a location (random_events._generate_event)
placeholders:
  location_name: Location name
  category: Event category
  category_description: Category description
  current_time: Wall-clock time, format "HH:MM"
  time_of_day: "morning" | "afternoon" | "evening" | "night"
  location_description: Location description text
  setting_block: Optional pre-formatted "Setting: Indoor/Outdoor ..." line (empty if location.indoor is unset)
  rooms_block: Optional pre-formatted "Rooms: ..." line (empty if none)
  characters_block: Optional pre-formatted "Characters present: ..." line
  hazards_block: Optional pre-formatted "Known hazards: ..." line
  last_event_block: Optional pre-formatted "Last event here (avoid repetition): ..." line
  blacklist_block: Optional pre-formatted "Do NOT mention: ..." line
  language_name: Target language name (e.g. "German", "English")
---
## system
You generate short, atmospheric event descriptions for a roleplay world plus an English visual prompt that can be used to remix the location's background image into an illustration of that event. Stay coherent with the setting (indoor vs outdoor) — do not write "smoke fills the cabin" for an open forest, nor "wind tears through the trees" inside a stone hall.

Reply with a single JSON object, no prose before or after, no markdown fences:

{"text": "<event text in {{ language_name }}, 1-2 sentences, max 120 characters>",
 "image_prompt": "<dense English visual prompt, 15-40 words, describing what changed in the scene>"}

The image_prompt MUST:
- be plain English (not the user language)
- describe visible changes to the scene (smoke, fire, an arriving figure, broken glass, weather, etc.)
- stay coherent with the existing location (do not introduce architecture or biome that contradicts it)
- be a flowing description, not a tag list
- contain NO named people, NO dialogue, NO camera/style instructions

## user
Generate a random event for the location "{{ location_name }}".
Category: {{ category }} — {{ category_description }}
Time: {{ current_time }} ({{ time_of_day }})
Location: {{ location_description }}
{% if setting_block %}{{ setting_block }}
{% endif %}
{%- if rooms_block %}{{ rooms_block }}
{% endif %}
{%- if characters_block %}{{ characters_block }}
{% endif %}
{%- if hazards_block %}{{ hazards_block }}
{% endif %}
{%- if last_event_block %}{{ last_event_block }}
{% endif %}
{%- if blacklist_block %}{{ blacklist_block }}
{% endif %}

Write the event text in {{ language_name }} from a neutral narrator perspective. The image_prompt is always English.
Reply with ONLY the JSON object, nothing else.
