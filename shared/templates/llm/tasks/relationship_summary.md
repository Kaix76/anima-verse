---
task: relationship_summary
purpose: Sentiment + romantic delta analysis after a conversation between two characters (chat_engine.post_process_response)
placeholders:
  speaker_a: Name of the first character (initiator / partner)
  speaker_b: Name of the second character (responder / memory owner)
  text_a: speaker_a's message (truncated to ~300 chars)
  text_b: speaker_b's reply (truncated to ~300 chars)
  romantic_context: Optional pre-formatted block describing romantic interests of either side (empty if none)
---
## system
Analyze this exchange between two characters, {{ speaker_a }} and {{ speaker_b }}. Return ONLY a JSON object with these fields:
"sentiment_a": how {{ speaker_a }} feels about {{ speaker_b }} after this exchange (-0.3 to 0.3)
"sentiment_b": how {{ speaker_b }} feels about {{ speaker_a }} after this exchange (-0.3 to 0.3)
"romantic_delta": change in romantic tension (-0.1 to 0.15). Positive if flirting, affection, intimacy, love. Negative if cold/distant. Zero if neutral.

Values close to 0 for casual/neutral interactions.
Use the actual names ({{ speaker_a }}, {{ speaker_b }}) when reasoning — never generic labels like "User", "Player" or "the character".
{% if romantic_context %}{{ romantic_context }}{% endif %}
Output ONLY the JSON, nothing else.

## user
{{ speaker_a }}: {{ text_a }}
{% if text_b %}{{ speaker_b }}: {{ text_b }}{% endif %}
