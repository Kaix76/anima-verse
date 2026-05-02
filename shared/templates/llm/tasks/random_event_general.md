---
task: random_event
purpose: Generate a random atmospheric event for a location (random_events._generate_event)
placeholders:
  location_name: Location name
  category: Event category
  category_description: Category description
  time_of_day: "morning" | "afternoon" | "evening" | "night"
  location_description: Location description text
  rooms_block: Optional pre-formatted "Rooms: ..." line (empty if none)
  characters_block: Optional pre-formatted "Characters present: ..." line
  hazards_block: Optional pre-formatted "Known hazards: ..." line
  last_event_block: Optional pre-formatted "Last event here (avoid repetition): ..." line
  blacklist_block: Optional pre-formatted "Do NOT mention: ..." line
  language_name: Target language name (e.g. "German", "English")
---
## system
You generate short, atmospheric event descriptions for a roleplay world. Reply with ONLY the event text.

## user
Generate a random event for the location "{{ location_name }}".
Category: {{ category }} — {{ category_description }}
Time of day: {{ time_of_day }}
Location: {{ location_description }}
{% if rooms_block %}{{ rooms_block }}
{% endif %}
{%- if characters_block %}{{ characters_block }}
{% endif %}
{%- if hazards_block %}{{ hazards_block }}
{% endif %}
{%- if last_event_block %}{{ last_event_block }}
{% endif %}
{%- if blacklist_block %}{{ blacklist_block }}
{% endif %}

Write ONE short event description (1-2 sentences, max 120 characters).
Write in {{ language_name }}.
Write from a neutral narrator perspective.
The event should feel natural for this location and time.
Reply with ONLY the event text, nothing else.
