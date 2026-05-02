---
task: relationship_summary
purpose: Extract romantic/sexual interests for a list of characters (models/relationship)
placeholders:
  char_descriptions: Bullet list of character descriptions ("- Name: personality...")
---
## system
You are a character relationship analyst. Be conservative.

## user
Given these fictional characters, extract their romantic/sexual interests as a short general description (5-15 words each). Be conservative — only include interests clearly stated or strongly implied.
Describe the TYPE of person they are attracted to, NOT specific character names.

Characters:
{{ char_descriptions }}

Return a JSON object where each key is a character name and the value is a short text describing their romantic/sexual preferences.
Use empty string "" if no romantic interests are apparent.
Examples: "women", "dominant men", "men and women, sexually open"
Output ONLY the JSON object, nothing else.
