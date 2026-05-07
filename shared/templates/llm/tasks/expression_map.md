---
task: expression_map
purpose: Generate an expression OR pose prompt fragment for unknown values (expression_pose_maps._llm_generate_prompt)
placeholders:
  prompt_type: "expression" or "pose"
  value: The unknown expression/pose value (e.g. "wistful", "kneeling")
---
## system

## user
{% if prompt_type == "expression" %}
Describe the facial expression for someone feeling '{{ value }}' in one short English sentence. Focus on eyes, eyebrows, mouth, jaw. Example: 'warm genuine smile, bright sparkling eyes, raised cheeks, relaxed brow'. Use only generic third-person language ("the person", "the eyes"). Never include proper names. Reply ONLY with the description, no explanation.
{% else %}
Describe the body pose for someone who is '{{ value }}' in one short English sentence. Focus on body position, arms, legs, posture. Example: 'sitting comfortably in a chair, legs crossed, hands resting on lap'. Use only generic third-person language ("the person", "their arms"). Never include proper names or refer to a partner by name — use "the partner" if a second person is implied. Reply ONLY with the description, no explanation.
{% endif %}
