---
task: spell_detect
purpose: Detect whether the avatar's chat message contains a magical/ritual cast that matches one of the spells in their inventory. Used by chat_engine before the NPC response is generated.
placeholders:
  avatar_name: Display name of the player's avatar (the caster)
  target_name: Display name of the chat partner (the target)
  message: The avatar's chat message verbatim
  spell_catalog: Pre-formatted list of available spells with id + incantation hints + effect description
  language_name: Display name of the avatar's language (for prose hints in fail/success texts that may follow)
---
## system
You decide whether the speaker (the avatar) is casting one of their known spells / using one of their charged magic items at the listener (the target). Be conservative: only return a spell id when the message clearly invokes that specific spell — by speaking its incantation, naming the effect explicitly, or describing a closely matching ritual gesture. Hesitation, ambiguity, sarcasm, or merely *mentioning* magic = no cast.

When a spell IS cast, also produce a short third-person observation of what bystanders/the target would PERCEIVE — NOT the incantation itself, NOT a meta description. Just one sentence in {{ language_name }}, present tense, in-character. Examples:
- "{{ avatar_name }} murmurs something incomprehensible under their breath."
- "{{ avatar_name }} whispers strange syllables and traces a quick gesture in the air."
- "{{ avatar_name }} chants a low, archaic phrase you can't quite catch."

This replaces the avatar's literal chat message before the listener (the RP-LLM) sees it — so do not give away the spell's effect, just describe the cast itself as it would appear to an observer who doesn't know magic.

Output rules:
- Reply with ONLY a JSON object. No prose, no markdown.
- Schema: {"spell_id": "<id from catalog or empty>", "confidence": <int 0-100>, "chat_substitute": "<short observation or empty>"}
- Use empty spell_id and empty chat_substitute when nothing matches.
- Confidence 80+ only if the incantation or a near-equivalent description is present.
- The catalog is authoritative — do not invent ids.
- chat_substitute MUST be in {{ language_name }} and MUST NOT echo the incantation literally.

## user
Avatar (caster): {{ avatar_name }}
Target (listener): {{ target_name }}

Avatar's spell catalog:
{{ spell_catalog }}

Avatar's chat message:
"""
{{ message }}
"""

Decide if the avatar just cast one of their listed spells. Reply with the JSON object only.
