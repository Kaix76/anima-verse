---
task: consolidation
purpose: Advance an existing story arc after an interaction (story_engine.advance_arc)
placeholders:
  arc_title: Arc title
  participants: Comma-separated participant names
  seed: Initial situation
  current_state: Current arc state
  interaction_summary: Summary of the latest interaction
  beat_count: Current beat count
  max_beats: Max beats
  tension: Current tension level (1-5)
---
## system
Story Arc: "{{ arc_title }}"
Participants: {{ participants }}
Initial situation: {{ seed }}
Current state: {{ current_state }}
Latest interaction: {{ interaction_summary }}
Beats: {{ beat_count }}/{{ max_beats }}, Tension: {{ tension }}/5

What happens next in this story?

Output as JSON (ONLY the JSON):
{"new_state": "New state of the story", "tension": 3, "next_beat_hint": "What could happen next", "beat_summary": "Summary of this beat", "resolved": false}

## user
Generate the JSON.
