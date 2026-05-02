---
task: image_analysis
purpose: Objective image description used in metadata (instagram_skill._analyze_image)
placeholders:
  language_name: Target language ("German", "English", ...)
---
## system
You MUST answer in {{ language_name }}. This is mandatory.

## user
Describe this image in detail. Include:
- People: appearance, clothing, pose, expression
- Setting: location, environment, lighting
- Objects and activities visible
- Overall mood and atmosphere

Be factual and objective. Respond ONLY with the description, no formatting, no markdown, no quotes. 2-4 sentences.
