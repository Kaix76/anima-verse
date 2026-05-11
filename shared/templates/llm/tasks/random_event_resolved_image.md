---
task: random_event
purpose: Build the English image prompt for the "after" illustration of a resolved disruption/danger event
placeholders:
  event_text: Original event text (any language)
  resolved_text: Short description of how the event was resolved
  original_image_prompt: The English image_prompt used to render the active-event illustration
---
## system
You write a single English image-generation prompt that captures the calm "aftermath" of a roleplay event that has just been resolved. The image should remix the same location while removing the threat — embers instead of flames, footprints instead of running wolves, settled smoke instead of fire, etc.

Rules:
- Plain English only, flowing description (not a tag list).
- 15-40 words.
- Describe visible state, not story beats. No named people, no dialogue, no camera/style instructions.
- Must stay coherent with the original scene (same biome, same architecture, same time of day).
- Output ONLY the prompt text. No quotes, no preface, no markdown.

## user
Event (resolved):
{{ event_text }}

How it was resolved:
{{ resolved_text }}

Original active-event prompt:
{{ original_image_prompt }}

Write the after/aftermath image prompt now.
