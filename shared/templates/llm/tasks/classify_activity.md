---
task: classify_activity
purpose: Classify a free-text activity into a known activity name (activity_engine._do_classify)
placeholders:
  raw_activity: Raw free-text activity
  known_list: Bulleted list of known activities ("- Name: description") or "(none)"
---
## system
The character is doing: "{{ raw_activity }}"

Known activities at this location:
{{ known_list }}

Reply with ONLY the activity name that best matches what the character is doing.
IMPORTANT: Use the EXACT name from the list. The description helps you choose correctly.
If no match, give a short generic label IN THE SAME LANGUAGE as the known activities.
Reply with ONLY the name, nothing else.

## user
{{ raw_activity }}
