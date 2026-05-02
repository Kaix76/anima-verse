# LLM Prompt Templates

All prompts that go to an LLM live as Markdown files under
`shared/templates/llm/` and are rendered at runtime via Jinja2
([app/core/prompt_templates.py](../app/core/prompt_templates.py)).

This document catalogs every active template, its purpose, where it's
called from, and which `task` (in `llm_routing`) it uses for model
selection.

## Layout

```
shared/templates/llm/
├── chat/
│   └── agent_thought.md           # AgentLoop slim system prompt
├── sections/                      # (currently empty after forced_thought removal)
└── tasks/
    ├── animation_prompt.md
    ├── consolidation_*.md
    ├── extraction_*.md
    ├── image_*.md
    ├── instagram_*.md
    ├── intent_*.md
    ├── outfit_generation.md
    ├── random_event_*.md
    ├── relationship_summary*.md
    ├── retrospect.md
    ├── secret_generation.md
    └── story_arc_*.md
```

## Tasks (rendered via `render_task(name, **vars)`)

### Memory & Summaries

| Template | Task | Caller | Purpose |
|---|---|---|---|
| `extraction_memory.md` | `extraction` | `memory_service.extract_memories_from_exchange` | After each chat turn: extract semantic facts + commitments |
| `extraction_chat_context.md` | `extraction` | `routes/chat._extract_context_from_last_chat` | Pull activity / outfit changes out of user-chat replies |
| `consolidation_daily.md` | `consolidation` | `memory_service._consolidate_episodics_to_daily` | Compress one day's episodic memories into a 3-5 sentence summary |
| `consolidation_weekly.md` | `consolidation` | `memory_service._consolidate_daily_to_weekly` | Compress a week's daily summaries (5-8 sentences) |
| `consolidation_monthly.md` | `consolidation` | `memory_service._consolidate_weekly_to_monthly` | Compress a month's weekly summaries (5-10 sentences) |
| `consolidation_today.md` | `consolidation` | `history_manager._create_daily_summary` | Roleplay summary of today's chat (5-8 sentences, past tense) |
| `consolidation_history_summary.md` | `consolidation` | `history_manager.create_summary` | Sliding-window summary of older chat history (2-3 sentences) |
| `consolidation_daily_diary.md` | `consolidation` | `routes/diary._generate_summary_sync` | First-person diary entry from a day's events |

### Intent / Routing

| Template | Task | Caller | Purpose |
|---|---|---|---|
| `intent_activity.md` | `intent_activity` | `activity_engine._do_classify` | Classify free-text activity into a known activity name |
| `intent_location.md` | `intent_location` | `scheduler_manager._llm_choose_location` | Character picks a location when schedule has `__llm_choice__` |

### Relationships

| Template | Task | Caller | Purpose |
|---|---|---|---|
| `relationship_summary.md` | `relationship_summary` | `chat_engine.post_process_response` | Sentiment + romantic-delta after a user-chat exchange |
| `relationship_summary_pair.md` | `relationship_summary` | `relationship_summary._generate_summary` | Narrative summary of one character's view of another |
| `relationship_summary_romantic_interests.md` | `relationship_summary` | `models/relationship.extract_romantic_interests` | One-time extraction of romantic preferences from personality text |

### Random World Events

| Template | Task | Caller | Purpose |
|---|---|---|---|
| `random_event_general.md` | `random_event` | `random_events._generate_event` | Atmospheric event for a location |
| `random_event_escalation.md` | `random_event` | `random_events._escalate_event` | Escalate an unanswered disruption/danger event |
| `random_event_secret_hint.md` | `random_event` | `random_events._try_generate_secret_hint_event` | Subtle event hint about a hidden secret |
| `random_event_validate_solution.md` | `random_event` | `random_events.validate_solution` | Check whether an action plausibly resolves an event |
| `random_event_solution_rp.md` | `thought` | `random_events._generate_solution_rp` | Character RP-describes how they resolve the event |

### Story Arcs

| Template | Task | Caller | Purpose |
|---|---|---|---|
| `story_arc_generation.md` | `consolidation` | `story_engine.generate_arc` | Generate a multi-character mini-storyline |
| `story_arc_advancement.md` | `consolidation` | `story_engine.advance_arc` | Advance an arc by one beat after an interaction |
| `story_arc_resolve.md` | `consolidation` | `story_engine.resolve_arc` | Close an arc with résumé + per-character outcomes |

### Image / Vision

| Template | Task | Caller | Purpose |
|---|---|---|---|
| `image_prompt_scene.md` | `image_prompt` | `routes/chat._generate_image_prompt` | Extract a visual scene from chat narrative |
| `image_prompt_improver.md` | `image_prompt` | `image_regenerate.enhance_prompt` | Modify an existing image prompt based on user feedback |
| `image_prompt_enhance.md` | `image_prompt` | `prompt_adapters._llm_enhance` | Workflow-specific stylistic rewrite of an image prompt |
| `image_analysis.md` | `image_analysis` | `instagram_skill._analyze_image` | Vision-LLM objective image description |
| `instagram_caption.md` | `instagram_caption` | `instagram_skill._generate_caption` | Vision-LLM Instagram post caption |
| `animation_prompt.md` | `instagram_caption` | `routes/instagram` + `routes/characters` (suggest-animate) | Image-to-video motion prompt |
| `expression_map.md` | `expression_map` | `expression_pose_maps._llm_generate_prompt` | Generate expression OR pose prompt fragment for unknown values |

### Skills (agent-callable)

| Template | Task | Caller | Purpose |
|---|---|---|---|
| `outfit_generation.md` | `outfit_generation` | `OutfitCreationSkill.execute` | Generate a coherent outfit as a list of pieces |
| `secret_generation.md` | `secret_generation` | `secret_engine.generate_secrets` | Generate plausible secrets for a character |
| `retrospect.md` | `consolidation` | `RetrospectSkill.execute` | Self-reflection — extract beliefs + improvement intentions |

## Top-level chat composites

### `chat/agent_thought.md`

The AgentLoop's slim system prompt. Pre-decision logic in
[app/core/thought_context.py](../app/core/thought_context.py) builds a
dict with only the blocks that have content (inbox, events,
assignments, general task, commitments, outfit-decision, story arc,
retrospective). The template renders nothing for empty blocks.

Section ordering reflects priority — what comes first gets more LLM
attention:

1. Identity + situation (always)
2. Inbox (unread chat-history messages)
3. Active events at location
4. Active assignments
5. General task (`character_task` from profile)
6. Open commitments
7. Outfit-decision (after location change or wake)
8. Active story arc
9. Retrospective (existing beliefs/improvements + boost hint when overdue)
10. Tools hint
11. Decision instruction

## Loader API

```python
from app.core.prompt_templates import render, render_task

# Task templates with `## system` / `## user` markers:
system_prompt, user_prompt = render_task("extraction_memory",
    user_display="Player",
    user_message="...",
    character_name="Hellena",
    ...)

# Plain templates (no system/user split):
text = render("chat/agent_thought.md", **context_dict)
```

`StrictUndefined` is enabled — missing placeholders raise loud errors
instead of silently rendering empty.

## Cross-references

- **Task → model routing:** [docs/llm-task-mapping.md](llm-task-mapping.md)
  for the `llm_routing` admin tab and how each task's model is picked
- **AgentLoop architecture:** [app/core/agent_loop.py](../app/core/agent_loop.py)
  — continuous worker, importance-weighted round-robin
- **Inbox model:** [app/core/agent_inbox.py](../app/core/agent_inbox.py)
  — chat_messages-backed, last_thought_at cutoff
