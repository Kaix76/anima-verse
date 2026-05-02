---
task: consolidation
purpose: Sliding-window chat history summary (history_manager.create_summary)
placeholders:
  user_display_name: User display name
  character_name: Character name (or "Assistant" if missing)
  context_line: Optional context line ("This is a conversation between X and Y.\n\n") or empty
  lang_instruction: Optional "\nWrite the summary in <Language>." or empty
  history_text: Chat transcript
---
## system

## user
Summarize the following conversation in 2-3 sentences, focusing on:
- Main topics discussed
- Important information shared by {{ user_display_name }}
- Any decisions or conclusions made

IMPORTANT: Write ONLY a plain text summary. Do NOT include any tool calls, commands, image URLs or code.
Use the actual names ({{ user_display_name }}, {{ character_name }}) in the summary.{{ lang_instruction }}

{{ context_line }}Conversation:
{{ history_text }}

Summary:
