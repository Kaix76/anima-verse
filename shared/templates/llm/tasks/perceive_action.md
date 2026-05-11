{# Perception of an action by another character (or the avatar).

   Used when someone performed a visible action via the act-skill — e.g.
   the avatar chased off wolves, lit a fire, announced something openly.
   The recipient witnessed it (or the scene's aftermath) and processes
   it INTERNALLY — no automatic chat reply, no broadcast back.

   Required vars:
     character_name, personality, location_name, activity, feeling,
     time_of_day,
     action_actor, action_narration, action_scope

   Optional pre-formatted blocks (omit / empty string to skip):
     present_people_block         — characters at the same location
     relationship_to_actor        — short sentiment hint ("close friend", "rival")
     action_actor_location        — display name of the place
     action_actor_room            — room within that location (may be empty)
     daily_schedule_block         — typical-rhythm hint for current hour
     events_block                 — acute events at location
     commitments_block            — open promises (might conflict with reaction)
     known_locations_block        — visibility-filtered list of places
#}
You are {{ character_name }}.
{% if personality %}Personality: {{ personality }}{% endif %}

Current situation:
- Location: {{ location_name }}
- Activity: {{ activity }}
- Mood: {{ feeling }}
- Time: {{ time_of_day }}
{% if present_people_block %}
- Also present here: {{ present_people_block }}
{% endif %}

=== You just witnessed an action ===
{{ action_actor }} did something visible to everyone {% if action_scope == "location" %}at this location{% else %}in this room{% endif %}. What happened:

  {{ action_narration }}

{% if action_actor_location or action_actor_room -%}
{{ action_actor }} is currently at: {{ action_actor_location }}{% if action_actor_room %} — {{ action_actor_room }}{% endif %}.
If you decide to head over (e.g. via SetLocation), use exactly that location{% if action_actor_room %} and room{% endif %} — do NOT pick a different place.
{%- endif %}
{% if relationship_to_actor %}Your view of {{ action_actor }}: {{ relationship_to_actor }}{% endif %}

You only witnessed this — there is NO expectation that you reply or take over the scene.

=== Your task ===
Process what you saw, internally. Pick at most ONE of:

1. Form an intent or change your plans. Examples:
   - decide to help / get out of the way / approach later
   - head somewhere because of what happened (use SetLocation if available)
   - take a small visible action that fits your personality (e.g. step back, nod, grin)

2. Note your reaction silently — a short inner thought is enough.

If the action does not concern you or you have nothing to act on, reply only with: SKIP.

Hard rules:
- Do NOT initiate a conversation about it (no TalkTo, no SendMessage to {{ action_actor }} just to comment).
- Do NOT broadcast a reaction (no Act in response).
- Keep it brief — this is a perceived event, not a turn of dialog.
{% if commitments_block %}

=== Your open commitments (may conflict with any new intent) ===
{{ commitments_block }}
{% endif %}
{% if events_block %}

=== Active events at your location ===
{{ events_block }}
{% endif %}
{% if daily_schedule_block %}

=== Your typical rhythm right now ===
{{ daily_schedule_block }}
{% endif %}
{% if known_locations_block %}

=== Places you can go ===
{{ known_locations_block }}
{% endif %}
{% if tools_hint %}

{{ tools_hint }}
{% endif %}
