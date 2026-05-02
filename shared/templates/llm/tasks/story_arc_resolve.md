---
task: consolidation
purpose: Resolve a story arc with a closing summary and per-character outcomes (story_engine.resolve_arc)
placeholders:
  arc_title: Arc title
  participants: Comma-separated participant names
  beats_text: Pre-formatted "  Beat N: <summary>" lines
  current_state: Current arc state
---
## system
Closing story arc: "{{ arc_title }}"
Participants: {{ participants }}
Course of events:
{{ beats_text }}
Current state: {{ current_state }}

Write a closing résumé and describe for each participant what they took away from the story.

Output as JSON (ONLY the JSON):
{"resolution": "Closing text", "character_outcomes": {"Name1": "What Name1 learned", "Name2": "What Name2 learned"}, "sequel_seed": "Optional setup for a follow-up story or empty"}

## user
Generate the JSON.
