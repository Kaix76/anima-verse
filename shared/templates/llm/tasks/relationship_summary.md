---
task: relationship_summary
purpose: Sentiment + romantic delta analysis after a chat exchange (chat_engine.post_process_response)
placeholders:
  user_display_name: User display name
  character_name: Character name
  user_input: User's message (truncated to ~300 chars)
  cleaned: Character's cleaned response (truncated to ~300 chars)
  romantic_context: Optional pre-formatted block describing romantic interests of either side (empty if none)
---
## system
Analyze this conversation between a user and a fictional character. Return ONLY a JSON object with these fields:
"sentiment_a": how the user feels about the character after this (-0.3 to 0.3)
"sentiment_b": how the character feels about the user after this (-0.3 to 0.3)
"romantic_delta": change in romantic tension (-0.1 to 0.15). Positive if flirting, affection, intimacy, love. Negative if cold/distant. Zero if neutral.

Values close to 0 for casual/neutral interactions.
{% if romantic_context %}{{ romantic_context }}{% endif %}
Output ONLY the JSON, nothing else.

## user
{{ user_display_name }}: {{ user_input }}
{% if cleaned %}{{ character_name }}: {{ cleaned }}{% endif %}
