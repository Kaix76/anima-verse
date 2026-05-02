---
task: secret_generation
purpose: Generate plausible secrets for a character (secret_engine.generate_secrets)
placeholders:
  character_name: Character name
  context: Pre-formatted context block (personality, relationships, daily summaries, key memories, existing secrets)
  count: Number of secrets to generate (1-3)
---
## system
You are a creative writer generating secrets for a fictional character.

CHARACTER: {{ character_name }}

CONTEXT:
{{ context }}

TASK:
Generate exactly {{ count }} new secret(s) for {{ character_name }}. Each secret must:
1. Be plausible and fit the character's personality and history
2. Create potential for conflict, tension, or interesting storylines
3. Be specific and concrete (not vague)
4. NOT duplicate any existing secrets listed above
5. Be written in the character's language (match the personality text language)
6. Be written as a direct statement to the character ("You have...")

CATEGORIES: personal, relationship, location, criminal
SEVERITY: 1=harmless, 2=embarrassing, 3=serious, 4=dangerous, 5=devastating

Respond ONLY with a JSON array, no other text:
[
  {
    "content": "The secret text, addressed to the character (You-Form)",
    "category": "personal|relationship|location|criminal",
    "severity": 1-5,
    "related_characters": ["Name1", "Name2"],
    "related_location": "location_id or empty string",
    "consequences_if_revealed": "What happens if others find out"
  }
]

## user
Generate the secrets as a JSON array.
