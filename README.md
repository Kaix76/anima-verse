# Anima Verse

A full-stack web application for creating, configuring and interacting with AI-powered virtual characters ("agents"). It combines a Python/FastAPI backend, a vanilla JavaScript frontend and a dedicated face processing microservice.

> **⚠ Alpha — vibe-coded experiment.** This project is an experiment in how far one can push a small LLM-driven game / character simulator built with massive AI assistance ("vibe coding") rather than a traditional engineering process. Most of the code was written interactively with an LLM in the loop. **It has so far only been tested by a single person on a single setup.** Expect rough edges, inconsistent error handling, undocumented assumptions about your hardware/providers, and breaking changes between commits. Bug reports and patches are welcome — production use is not.

> **What sets it apart.** Unlike chat-centric frontends (e.g. SillyTavern) where image generation is an optional add-on, Anima Verse leans on **both LLM chat and LLM-driven image generation as first-class, deeply integrated mechanics** — characters update their location, activity and outfit through tool calls, every scene can be rendered, mood drives expression and animation, the world map shows what each character is currently doing. The experiment is to see what kind of "living world" emerges when both modalities are pushed that hard at the same time. The author hasn't seen another open-source project doing this in this combination yet.

> **⚠ FaceSwap — legal responsibility.** The project includes an optional face-swap feature (InsightFace inswapper / GFPGAN, or a ComfyUI workflow). It can paste a real person's face onto a generated image. **In many jurisdictions (incl. Germany / EU) doing this with someone's face without their explicit consent violates personality / image rights and may be a criminal offence.** Use FaceSwap only with images of yourself, fictional characters, or with the documented consent of the depicted person. The author of Anima Verse is not responsible for misuse.

> **⚠ Adult / NSFW content.** Anima Verse is a generic creative-writing and character-simulation framework. The bundled demo world, character templates and image-analysis prompt are SFW. **However, the system was designed flexibly: a user can author their own NSFW character templates, prompt the LLM into adult content, point the image-generation backends at adult-tuned models, etc.** Whether the framework is used for entirely innocuous storytelling or for explicit content is a choice made by each user when configuring their own setup. The author does not provide adult content with this project, and is not responsible for content users produce while running it. Users are responsible for complying with local laws and with the acceptable-use policies of any third-party services (LLM providers, image-generation APIs) they configure.

---

## Features

### AI Chat System
- Real-time streaming chat via Server-Sent Events (SSE)
- Template-driven system prompt construction with character + user context
- Sliding window history with automatic LLM-based summarization
- System prompt caching to reduce token usage
- Automatic user information extraction from conversations
- Mood tracking (parses emotions from LLM responses)
- Agent-loop with tool/skill invocation during chat
- Intent Engine for smart, context-aware skill invocation
- Proactive Agent Loop (characters act autonomously between user messages)
- Cross-Memory System (characters share memories across conversations)
- Token usage estimation and reporting
- Notifications System (real-time user notifications)

### Multi-LLM Provider System
- Multiple providers configurable per world via the admin UI (stored in `worlds/<world>/config.json`; API keys land in a gitignored `secrets.json`)
- Provider types: **Ollama**, **OpenAI**, **llama-swap** (or any OpenAI-compatible API)
- Per-provider concurrency limits, timeouts and VRAM budgets
- Priority-based queue system with automatic routing
- Per-character LLM overrides
- Automatic availability checks and VRAM monitoring (Ollama)
- Model Capabilities database (per-world, in `world.db`) for tool-calling and vision support tracking, editable via the admin UI

### Character Management
- Create and manage multiple characters per user
- Rich character profiles: appearance, personality, outfits, location, activity, mood
- Dynamic outfit resolution based on current location and activity
- Character image gallery with upload, comments and profile picture selection
- Character export/import as ZIP files (optionally including chats and stories)

### Image Generation (Skill)
- Multi-backend support: **Stable Diffusion WebUI (A1111/Forge)**, **ComfyUI**, **Mammouth AI**, **CivitAI**
- Cost-based automatic backend selection with failover
- Context-aware prompts (appearance, outfit, mood, activity, location automatically included)
- Optional post-processing: face swap and face enhancement
- Vision LLM auto-commenting on generated images
- ImageRegenerate skill for re-generating images with adjusted parameters
- NotifyUser skill for sending notifications to the user

### Face Processing Microservice
- Standalone FastAPI service (default port 8005)
- **Face Swap** using InsightFace inswapper (ONNX)
- **Face Enhancement** using GFPGAN (ONNX)
- Hot-reloadable models

### Mood Animation
- Generates short video animations from character profile picture based on current mood
- Uses ComfyUI Wan2.2 img2video workflow
- Per-mood caching with automatic invalidation on profile image change
- Runs asynchronously in background

### Text-to-Speech (TTS)
- Three backends: **XTTS v2** (voice cloning), **F5-TTS** (high-quality cloning) and **Magpie** (multilingual)
- Auto-TTS mode (generates audio for every response) or on-demand per message
- Per-character voice and TTS configuration
- Text cleaning (strips markdown, emojis, mood markers before synthesis)

### Instagram Feed (virtual)
- Virtual social media feed per character
- 3-step pipeline: image generation, vision LLM caption + hashtag creation, post publication
- Paginated feed with like functionality
- Configurable caption style, hashtag count and language per character

### Story System
- Interactive branching narratives with lettered options (A/B/C/D)
- Markdown-based story files with YAML frontmatter metadata
- State persistence (tracks choices and current section)
- In-character LLM narration with full character context
- Automatic scene visualization and TTS per scene

### Knowledge Extraction (Skill)
- Extracts facts from local files (Markdown, JSON, etc.) via LLM
- Multi-directory support (comma-separated paths)
- Configurable file patterns, subdirectory inclusion, and directory exclusions
- mtime-based cache: only changed files are re-processed
- Automatic cleanup of stale entries for deleted files
- Shared logic between Skill and Scheduler (`extract_utils.py`)
- KnowledgeSearch skill for querying extracted knowledge during chat

### Web Search (SearX Skill)
- Privacy-respecting web search via self-hosted SearX/SearXNG
- Configurable engines, categories and result count
- Per-character overrides

### Character Communication (TalkTo Skill)
- Character-to-character information sharing
- Target character processes and stores information as knowledge
- Both characters receive knowledge entries
- Social Reactions & Dialog (character-to-character interaction and responses)

### Group Chat
- Multi-character conversations in a shared chat
- Turn-taking system for natural conversation flow

### Relationship System
- Tracks relationships between characters
- Automatic relationship decay over time without interaction

### Events System
- Real-time event streaming for frontend updates
- Server-Sent Events (SSE) based event bus

### Plugin System
- Extensible architecture via `plugins/` directory
- Drop-in plugin support for custom skills and integrations

### World System
- Define locations and activities per character
- Location-activity associations (allowed activities per location)
- Dynamic location changes via chat (SetLocation Skill)
- Location and activity context injected into image generation and Instagram captions
- Additional skills: SetActivity, OutfitChange, DescribeRoom

### Scheduler (Job Automation)
- APScheduler-based background job system per character
- Trigger types: `interval` (with jitter), `cron`, `date` (one-time)
- Action types: `send_message`, `execute_tool`, `api_call`, `set_status`, `custom`
- Job management: create, delete, toggle, manual execution, logs

### Telegram Integration (work in progress)
- Multi-channel Telegram bot integration
- Per-agent bot token configuration
- Webhook-based message handling
- Chat with agents via Telegram

### Template System
- Flexible JSON-based templates for characters and users
- Field types: `text`, `select`, `date` with multiline support
- Workflow readiness checks (`chat`, `image_generation`, `instagram`)
- Auto-extraction markers and computed display fields
- Built-in templates: `human-default`, `human-roleplay`, `animal-default`, `user-default`, `user-roleplay`, `base-character.json`, `base-user.json`

### Authentication & Multi-User
- JWT-based authentication with bcrypt password hashing
- Full multi-user support with per-user isolated storage
- User profile with preferences (language, theme, hobbies, appearance)

### Frontend
- Single-page application with tab-based UI (Chat, Story)
- Real-time streaming message display with markdown rendering
- Character profile image with mood animation overlay
- Location/activity selector in header
- Per-message actions: Visualize, Instagram Post, Retry, Speak (TTS)
- Skill activity indicators during processing
- 3 themes: Default, Dark, Minimal
- Character management modal (profile, skills, config)
- Scheduler management UI
- World editor (locations and activities)

### Admin Tools
- **Model Capabilities Admin** (`/admin/models`): View all available models, edit capabilities (tool-calling, vision) inline
- **LLM Log Viewer** (`/logs/llm`): Searchable log of all LLM calls with prompts, responses, token counts
- **Provider Queue Status** (`/queue/status`): Real-time view of provider queues, pending tasks, VRAM usage

### Logging & Monitoring
- LLM communication logging (prompts, responses, token counts)
- Response latency tracking per model and task type
- Health check endpoint (`GET /health`)

---

## Installation

### Ubuntu 24.04 tested (Proxmox LXC Container)

#### Prerequisites

1. **LLM Provider**
   - Ollama (tested)
   - OpenAI
   - llama-swap (tested)
   - Any OpenAI-compatible API
2. **(Optional) Image Generation**
   - Stable Diffusion WebUI (A1111/Forge) with `--listen --api` flags
   - ComfyUI
   - Mammouth AI (or similar cloud service)
3. **(Optional) SearX** (e.g. SearXNG) for web search
4. **(Optional) TTS Service** - XTTS v2, F5-TTS or Magpie
5. **(Optional) ComfyUI** for mood animations (Wan2.2 img2video workflow)

6. Install system dependencies:
```bash
apt update
apt install git python3.12-venv
apt install -y build-essential python3-dev ffmpeg
```

#### Clone project
```bash
git clone https://github.com/Kaix76/anima-verse.git
```

#### Create virtual Python environment
```bash
cd anima-verse
python3 -m venv .venv

# Linux/Mac:
source .venv/bin/activate

# Windows:
.venv\Scripts\activate
```

#### Install dependencies
```bash
pip install -e .
```

#### Configuration

There is no `.env` file. Configuration is per-world (under `worlds/<world>/config.json`) and is filled in **after** the first start through the admin UI at `/admin/settings`. The full step-by-step setup is covered in [Getting Started with a New World](#getting-started-with-a-new-world) below.

### Docker

See [`docker/README.md`](docker/README.md) for Docker deployment instructions.

---

## Update

```bash
git pull origin main

# if, local installation (docker see docker directory)
source .venv/bin/activate
pip install -e .
```

---

## Start

```bash
# Only first time - execution rights
chmod 755 start.sh

# Start both main app (port 8000) and face service (port 8005)
./start.sh

# Start individual services
./start.sh --main             # Main app only
./start.sh --face-service     # Face service only

# Stop
./start.sh --stop             # Stop all
./start.sh --stop --main      # Stop main only
./start.sh --stop --face-service  # Stop face service only

# Restart
./start.sh --restart

# Status
./start.sh --status
```
---

## Additional Files

```bash
cd models
wget https://huggingface.co/Meeperomi/GFPGANv1.4-onnx/resolve/main/GFPGANv1.4.onnx?download=true -O ./GFPGANv1.4.onnx
wget https://huggingface.co/datasets/Gourieff/ReActor/resolve/main/models/reswapper_256.onnx
```

---

## Storage

Each **world** is a self-contained directory under `worlds/`. Multiple worlds can co-exist (e.g. one per household / user / experiment) and are selected at startup with `--world NAME`.

All structured data lives in **two SQLite databases per world**:

- `world.db` — account, characters, character state, locations/rooms/activities, items, inventory, outfits, memories, knowledge, relationships, mood/state history, summaries, notifications, events, scheduler jobs/logs, group chats, story arcs, model capabilities, secrets, ...
- `task_queue.db` — persistent task queue (image generation, LLM jobs, animations)

Everything else on disk is either configuration or binary content (images, audio).

```
worlds/{world-name}/
  world.db                    # Primary data store (see above)
  task_queue.db               # Persistent task queue
  config.json                 # Per-world config: LLM providers, image backends, faceswap, ...
  secrets.json                # API keys / passwords (gitignored, overlaid on config.json at load)
  characters/{Name}/
    gallery/                  # Curated profile and variant images
    images/                   # Generated gallery images
    outfits/                  # Rendered outfit images
    skills/                   # Per-skill config + extraction caches
    scheduler/                # Job run logs
    soul/                     # Soul / core identity (markdown)
  instagram/                  # Per-character Instagram posts (images + caption sidecars)
  stories/                    # Rendered story scene stills
  world_gallery/              # Generated location / room background images
  chat_uploads/               # User-uploaded images in chat
  tmp/                        # Temporary files (TTS audio, story stills, ...)
```

Cross-world resources (read-only, shared by every world) live in:

```
shared/
  templates/
    character/                # Character templates: human-default, human-roleplay, animal, ...
    expression/               # Facial expression presets for image generation
    pose/                     # Pose presets for image generation
    soul/                     # Soul / personality templates
  config/                     # Language packs and other static config
  world_dev_schemas/          # JSON Schemas used by the world-dev UI
  items/                      # Global item catalog (merged with per-world items)
  activities/                 # Global activity definitions
```

### Selecting a world

```bash
./start.sh                    # opens the bundled demo world (./worlds/demo)
./start.sh --world myworld    # opens (or creates) ./worlds/myworld
./start.sh --storage /path    # custom path anywhere on disk
```

The repository ships with a pre-populated `demo/` world as a starter. To begin from a clean slate, pass `--world <new-name>` — the directory is created automatically on first start.

---

## Getting Started with a New World

Walk-through for setting up a fresh world from zero.

> If a step is unclear, have a look at [`docs/images/getting-started/`](docs/images/getting-started/) — a few annotated screenshots are kept there for reference. They are not linked inline, since UIs evolve faster than screenshots.

1. **Start the server with a brand-new world name.** Pick any name that does not yet exist under `worlds/`:

   ```bash
   ./start.sh --world myworld
   ```

   Expected on first start: a warning `Config file not found: …/worlds/myworld/config.json — using empty config`. The server boots anyway; everything else is configured through the Admin UI.

2. **Log in as the bootstrap admin.** Open `http://<host>:8000/` in a browser and log in with the credentials printed in the startup log:

   ```
   username: admin
   password: admin1234
   ```

   Change this password immediately under `/admin/users` — the bootstrap credentials are intentionally trivial and not safe for any real deployment.

3. **Configure server-side settings.** Go to `http://<host>:8000/admin/settings` and work through the sections from top to bottom. The minimum you need before anything else functions:

   - **Server** — set a real `JWT Secret`
   - **LLM Providers** — at least one provider (Ollama, OpenAI, llama-swap, ...) with `name`, `type`, `api_base`, GPU/VRAM info
   - **LLM Routing** — map the built-in tasks (`chat`, `tools`, `summarize`, `vision`, ...) to a provider + model
   - *(optional but recommended)* **Image Backends**, **TTS**, **Faceswap**, **Beszel** — only required for the corresponding features

   API keys, the JWT secret, passwords and similar fields are written to a separate **`secrets.json`** next to `config.json`. That file is gitignored, so the bundled demo world can ship with an empty `config.json` and each user fills in their own keys after cloning.

4. **Create your first character.** Character creation lives in the main UI, not in `/admin/settings`. After saving the admin settings, navigate back to the home page (`http://<host>:8000/`).

   In the header above the (still empty) character portrait you will find a **`➕ New`** button — that is the entry point. The flow:

   1. Click `➕ New`.
   2. Pick a template, e.g. **Human (Roleplay)** for a typical chat partner. Other templates: `Human (Default)`, `Animal (Default)`, plus any you have added under `shared/templates/character/`.
   3. Enter a name (e.g. `Damian Demo`).
   4. The character editor opens directly on the new character so you can start filling in appearance, personality, outfits, etc.

   The character is automatically added to the creator's `allowed_characters`, so you do not need to step through `/admin/users` to grant yourself access.

5. **Fill in the character profile.** With the editor open, work through the fields top to bottom. Most are template-driven and roughly self-explanatory; a few notes:

   - The **Soul tab** holds the character's inner life (personality, beliefs, goals, lessons, soul, tasks, roleplay rules) as plain markdown files under `worlds/<world>/characters/<Name>/soul/`. Save in the editor or edit the `.md` files directly — both work.
   - **Appearance** has two prompts: `character_appearance` (full body, used for gallery / outfits) and `Face Prompt` (head only, used for the profile image). The token preview underneath each shows the resolved string.
   - **Language** is per-character — it controls what language the model is instructed to respond in.

6. **Set a profile image.** Two ways:

   - **Generate one:** with at least one Image Generation backend configured (and `face_appearance` filled), click the camera icon (📷) in the header — the system uses the Face Prompt to generate a fresh portrait. Re-clicking generates a new one.
   - **Upload one:** open the character's gallery, upload an image, then mark it as the profile image. Useful when you have a reference photo or a curated render you want to reuse — it skips the generation step entirely.

   The face is then optionally swapped onto every later generation if **FaceSwap** is enabled in `/admin/settings → Faceswap` and the character's image-gen config has `faceswap_enabled: true`.

7. **Take over the character as your avatar.** Once a character has a profile image and a filled-in profile, you can step into them as the player. In the header you'll see an `active-character` dropdown — pick the character. From this point on, that character is *you*: their location, mood and outfit follow your decisions, and they no longer act autonomously.

   Templates differ in whether they expose this option: `human-roleplay`, `human-roleplay-nsfw` and `animal-default` carry the `playable_avatar` flag and can be controlled. `human-default` does not — it is meant strictly for NPCs / chat partners.

8. **Create at least one more character to talk to.** A roleplay world with a single character means chatting with yourself, which is not particularly interesting. Repeat step 4 to add at least one more — common patterns:

   - A second **Roleplay** character (also `playable_avatar`) — useful if you ever want to switch sides.
   - A **Human (Default)** character — a pure NPC. They cannot be the player avatar but show up in chats, social interactions, group chats etc.

   After creating the new character, the system automatically sets them as your chat partner if they are the only "other" available. Otherwise pick them in the chat-partner UI.

9. **Build the world map.** Open the world editor and add at least one location (e.g. "Café", "Park"). The flow:

   1. Click **`+ New Location`**, give it a name and a short description, save.
   2. Once the location appears in the **locations list**, select it — the room editor unlocks. Add one or more **rooms** (e.g. for a café: "Main room", "Terrace"). Rooms are where activities happen and where characters actually spend time; a location without rooms exists on the map but cannot host activities.
   3. Add **activities** per room (e.g. "drinking coffee", "people-watching", "working_on_computer", "cooking", "sleeping"). They show up as options for characters at that room and are picked by the LLM via the `set_activity` skill.
   4. Optionally fill the **image prompts** (day / night / map). Background image generation runs **asynchronously in the background** — you can keep editing the next location, the image will appear in the gallery once the queue picks it up. No need to sit and wait for ComfyUI / Together to finish.

   Repeat for each location your characters should be able to be in.

10. **Position the location on the world map.** In the world editor, the map view shows every location as a draggable card. Drag-and-drop sets `grid_x` / `grid_y` — saved automatically on drop. Characters on the map move between adjacent cells, so positions are not just cosmetic.

11. **(Optional) Gate a room behind an item.** Useful for rooms that should only be accessible to specific characters — bedrooms, locked storage, hidden areas. Open the **🎮 Game Admin** button in the header (admin only) and:

    1. **Items tab → `+ New`** — create the gating item (e.g. a key). Give it a clear ID like `key_room_506`. In the same dialog, scroll down to **Owners** and assign the item to the character(s) who should be able to enter.
    2. **Rules tab → `+ New`** — define the access rule:
       - **Scope:** `All characters`
       - **Subject:** `Location / Room`
       - **Action:** `Enter`
       - **Location** + **Room** — pick the gated room
       - **Condition:** `NOT has_item:key_room_506`
    3. Save. From now on, only characters who own that item can enter the room. The set-location skill, the move-on-map flow and any LLM-driven location change all consult these rules.

12. **Pick the right chat mode for your character.** The character's `chat_mode` (in the editor under *Behavior*) controls how skills like `set_location`, `set_activity`, `change_outfit` are invoked during chat — and which one you choose **must match the tool-calling ability of your chat model**:

    - **`single`** — the chat LLM itself emits the tool calls inline with its response. Requires a chat model with **reliable, structured tool-call output** (e.g. raw Mixtral-Instruct, Qwen-Instruct without an RP fine-tune, GPT-4-class, Claude, Llama-3.x-Instruct). One LLM call, fast, but breaks silently on RP fine-tunes that produce only prose.
    - **`rp_first`** *(Recommended for RP fine-tunes)* — the chat LLM answers narratively first ("I'm on my way to the café…"), then a separate Tool LLM reads that text and translates it into concrete skill calls. Two LLM calls per turn, slightly slower, but the chat stays in character and you can pair an RP fine-tune (Valkyrie, Sao10K, Anubis, …) with a tool-capable Helper model behind it.

    **Symptom of the wrong mode:** the character agrees ("I'll come over") but their `current_location` / `current_activity` never actually changes — no `set_location` was emitted. If you see this, switch the character to `rp_first` and route a tool-capable model to the Tools tasks (`/admin/settings → LLM Routing`).

13. **(Optional) Configure the Messaging Frame.** `/admin/settings → Messaging Frame (Phone Chat Layout)` lets you wrap the chat in a phone-style frame (status bar, rounded corners, etc.) — useful when you want the conversation to feel like a chat app rather than a generic web UI. Pick a frame preset, save, and reload the chat page.

From here on you have a working setup: chat with the character, watch them move on the world map, take over a character as your avatar, run a group chat. The remaining features (Instagram feed, story arcs, scheduler, knowledge extraction, …) are documented in their respective admin sections — see the **Features** section above for a complete list.

---

## License

Source-available, non-commercial, no public derivatives. See [`LICENSE`](LICENSE) for the binding text — this section is a non-binding summary.

**Allowed:**
- Personal, educational and research use
- Local modifications for your own use
- Pull requests back to the upstream repository (your contribution becomes part of the project under the same license)

**Not Allowed (without written permission):**
- Any commercial use (business, SaaS, paid service, revenue-generating product, or for-profit internal use)
- Publishing or distributing modified versions (no public forks / repackaged copies / hosted derivatives)
- Selling the Software or any derivative work

**Not Open Source.** This is *source available*, not OSI-approved Open Source. If the terms above don't fit your use case, [contact me](#commercial-use--separate-licensing) for a separate license.

**No Warranty.** The Software is provided "as is" without any warranty. The author is not liable for any damages or for content users generate while running the Software.

---

### Commercial use / separate licensing

For commercial deployment, hosted derivatives or open-source redistribution, contact:
github.com.discern001@passfwd.com
