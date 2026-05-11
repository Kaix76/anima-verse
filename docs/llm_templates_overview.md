# LLM Prompt Templates Overview

## Template Structure and Organization

The LLM prompt templates are organized under `shared/templates/llm/` and follow a structured hierarchy:

### Directory Structure
```
shared/templates/llm/
├── chat/                 # Top-level chat/composite templates
│   ├── agent_thought.md   # AgentLoop system prompt
│   └── agent_thought_in_chat.md
├── tasks/                # Task-specific templates (mainly used by LLMs)
│   ├── extraction_memory.md     # Memory extraction from chat
│   ├── consolidation_today.md   # Daily roleplay summary
│   ├── image_prompt_enhance.md  # Image prompt enhancement
│   └── ... (many more task templates)
├── skills/               # Agent-callable skill templates
│   ├── image_generation.md      # Image generation skill
│   ├── set_location.md          # Location change skill
│   └── ... (other skill templates)
└── sections/             # Reusable components (currently empty)
```

### Template Format

Templates follow a consistent structure with YAML frontmatter that defines:
1. `task` - The LLM task this template belongs to
2. `purpose` - Description of what the template does
3. `placeholders` - Variables that must be provided when rendering

Each template has:
- **YAML frontmatter**: Contains metadata about the template
- **System/User sections**: Marked with `## system` and `## user` (for task templates)
- **Plain text body**: Content rendered as Jinja2 templates

## Template Usage Patterns

### Task Templates (tasks/)
Task templates are primarily used by the LLM system and are split into system and user prompts:
- `render_task()` function splits templates into system and user prompts
- Used by services like `memory_service`, `chat_engine`, `story_engine`
- Common tasks include: `extraction`, `consolidation`, `image_prompt`, `relationship_summary`

### Skill Templates (skills/)
Skills are agent-callable functions that can be invoked directly:
- Stored in `skills/` directory with name + description metadata
- Example: `image_generation.md`, `set_location.md`, `send_message.md`

## Key Template Examples

### extraction_memory.md
Used by `memory_service.extract_memories_from_exchange` to extract semantic facts and commitments from chat exchanges.
- Extracts memories in JSON format
- Handles facts vs commitments appropriately
- Supports specific formatting requirements for memory storage

### consolidation_today.md
Generates daily roleplay summaries for characters.
- Creates narrative summaries in past tense
- From character's perspective
- Focuses on key events and emotional moments

### image_prompt_enhance.md
Used for enhancing deterministic prompts with workflow-specific instructions.
- Works with `prompt_adapters._llm_enhance`
- Maintains all factual content while applying style guidance

## Loading and Rendering

Templates are loaded via `app/core/prompt_templates.py`:
- Uses Jinja2 templating engine with custom settings
- Supports frontmatter stripping for LLM prompts
- Validates missing placeholders with `StrictUndefined`
- Renders system/user prompts for task templates using `render_task()`

## Integration Points

Templates are integrated throughout the system:
- `app/core/prompt_templates.py` - Main loader
- `app/core/thought_context.py` - Uses agent_thought.md
- `app/core/agent_loop.py` - Core loop that uses templates
- Various routes and services call specific templates