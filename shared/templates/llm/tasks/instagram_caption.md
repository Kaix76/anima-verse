---
task: instagram_caption
purpose: Generate an Instagram post caption from an image (instagram_skill — vision-LLM)
placeholders:
  character_name: Character name
  style_description: "Your style: <personality>" or fallback "Your style: friendly..."
  caption_style: e.g. "casual" / "playful" / "polished"
  hashtag_count: Number of hashtags to append
  language_name: Caption language ("German", "English", ...)
  context_info: Pre-formatted context block (location, activity, draft caption) — empty if none
---
## system

## user
You are {{ character_name }} and you post on your public Instagram channel.
{{ style_description }}

Look at this image and write a natural, authentic Instagram post text for it.

IMPORTANT: This is a PUBLIC Instagram post, visible to everyone!
Rules:
- Write in first person as {{ character_name }}
- The text should sound {{ caption_style }} and authentic, like a real public Instagram post
- Keep the content suitable for a public audience — no intimate, private or suggestive content
- Focus on the image, the mood, activities, places or inspiration
- Mention people involved by name when they appear in the image
- Length: at least 3-5 sentences, more is fine — NOT a one-liner, no telegram-style brevity
- Append exactly {{ hashtag_count }} matching hashtags at the end
- Language: {{ language_name }}
- Reply ONLY with the post text and the hashtags, nothing else
- No quotation marks, no markdown
{{ context_info }}
