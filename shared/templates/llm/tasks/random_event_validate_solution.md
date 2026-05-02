---
task: random_event
purpose: Validate whether a roleplay action plausibly resolves an event (random_events.validate_solution)
placeholders:
  event_text: Event text being resolved
  event_category: Event category
  actor_with_caps: Actor name annotated with relevant trait hints
  joint_block: Optional " (together with X, Y)" string — empty if none
  solution_text: The action attempted
---
## system
You validate whether a roleplay action resolves an event. Reply ONLY with JSON.

## user
An event is happening: "{{ event_text }}"
Category: {{ event_category }}

{{ actor_with_caps }}{{ joint_block }} attempted to resolve it with this action:
"{{ solution_text }}"

Evaluate plausibility: does this action realistically resolve the event?
Consider:
- For a fire: extinguishing/evacuating = resolve; running away = no
- For a break-in: confronting/calling police = resolve; hiding = no
- For a water leak: shutting main valve/plumber = resolve; watching = no
- Character traits matter on edge cases: a 'brave' character pulls off risky interventions; an 'anxious' character fails when courage is needed; 'attentive' helps with subtle/social events; 'exhausted' reduces effectiveness.
Generally: resolution requires ACTIVE, EFFECTIVE action — not just awareness.

Reply with ONLY a JSON object (no prose): {"resolved": true|false, "reason": "<short reason>"}
