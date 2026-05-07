# Konfigurations-Defaults

> **Note:** Diese Liste wird automatisch aus dem Quellcode extrahiert.

Alle konfigurierbaren Parameter mit ihren Standardwerten. Diese können über `config.json` im Welt-Verzeichnis oder als Umgebungsvariablen überschrieben werden.

---

## Sicherheit

| Variable | Default | Datei |
|---|---|---|
| `JWT_SECRET` | `your-secret-key-change-in-production` | app/core/auth.py |

---

## Service URLs & Ports

| Variable | Default | Datei |
|---|---|---|
| `FACE_SERVICE_URL` | `http://localhost:8005` | app/server.py, app/core/dependencies.py, app/skills/face_client.py |
| `FACE_SERVICE_PORT` | `8005` | face_service/server.py |
| `SKILL_SEARX_URL` | `http://localhost:8888` | app/skills/searx_skill.py |
| `TELEGRAM_API_URL` | `https://api.telegram.org/bot` | app/models/telegram_channel.py |
| `PORT` | `8000` | app/scheduler/scheduler_manager.py, app/core/intent_engine.py |

---

## TTS (Text-to-Speech)

| Variable | Default | Datei |
|---|---|---|
| `TTS_ENABLED` | `false` | app/core/tts_service.py |
| `TTS_AUTO` | `false` | app/core/tts_service.py |
| `TTS_BACKEND` | `xtts` | app/core/tts_service.py |
| `TTS_FALLBACK_BACKEND` | `` | app/core/tts_service.py |
| `TTS_CHUNK_SIZE` | `0` | app/core/tts_service.py |
| `TTS_XTTS_URL` | `http://localhost:8020` | app/core/tts_service.py |
| `TTS_XTTS_SPEAKER_WAV` | `` | app/core/tts_service.py |
| `TTS_XTTS_LANGUAGE` | `de` | app/core/tts_service.py |
| `TTS_MAGPIE_URL` | `http://localhost:9000` | app/core/tts_service.py |
| `TTS_MAGPIE_VOICE` | `` | app/core/tts_service.py |
| `TTS_MAGPIE_LANGUAGE` | `de-DE` | app/core/tts_service.py |
| `TTS_COMFYUI_SKILL` | `` | app/core/tts_service.py |
| `TTS_COMFYUI_MODE` | `voiceclone` | app/core/tts_service.py |
| `TTS_COMFYUI_WORKFLOW_VOICECLONE` | `./workflows/tts_voiceclone_workflow_api.json` | app/core/tts_service.py |
| `TTS_COMFYUI_WORKFLOW_VOICEDESC` | `./workflows/tts_voicedesc_workflow_api.json` | app/core/tts_service.py |
| `TTS_COMFYUI_WORKFLOW_VOICENAME` | `./workflows/tts_voicename_workflow_api.json` | app/core/tts_service.py |
| `TTS_COMFYUI_MAX_WAIT` | `300` | app/core/tts_service.py |
| `TTS_COMFYUI_POLL_INTERVAL` | `1.0` | app/core/tts_service.py |
| `TTS_F5_URL` | `http://localhost:7860` | app/core/tts_service.py |
| `TTS_F5_SPEED` | `1.0` | app/core/tts_service.py |
| `TTS_F5_NFE_STEPS` | `32` | app/core/tts_service.py |
| `TTS_F5_REMOVE_SILENCE` | `false` | app/core/tts_service.py |

---

## LLM & Chat

| Variable | Default | Datei |
|---|---|---|
| `LLM_REQUEST_TIMEOUT` | `120` | app/core/llm_router.py, app/skills/image_generation_skill.py, app/skills/instagram_skill.py |

---

## Memory & History

| Variable | Default | Datei |
|---|---|---|
| `MEMORY_SHORT_TERM_DAYS` | `3` | app/utils/history_manager.py |
| `MEMORY_MID_TERM_DAYS` | `30` | app/utils/history_manager.py |
| `MEMORY_LONG_TERM_DAYS` | `90` | app/utils/history_manager.py |
| `MEMORY_MAX_PROMPT_ENTRIES` | `20` | app/models/memory.py |
| `MOOD_HISTORY_MAX_ENTRIES` | `500` | app/models/memory.py |
| `CHAT_HISTORY_MAX_MESSAGES` | `100` | app/utils/history_manager.py |
| `CHAT_SESSION_GAP_HOURS` | `4` | app/utils/history_manager.py |
| `DAILY_SUMMARY_DAYS` | `7` | app/utils/history_manager.py |
| `MEMORY_COMMITMENT_MAX_DAYS` | `7` | app/core/memory_service.py |
| `MEMORY_COMMITMENT_COMPLETED_DAYS` | `3` | app/core/memory_service.py |
| `MEMORY_MAX_SEMANTIC` | `50` | app/core/memory_service.py |

---

## Knowledge

| Variable | Default | Datei |
|---|---|---|
| `KNOWLEDGE_MAX_ENTRIES` | `50` | app/models/knowledge.py |
| `KNOWLEDGE_MAX_PROMPT_ENTRIES` | `20` | app/models/knowledge.py |

---

## Face Enhancement / Swap

| Variable | Default | Datei |
|---|---|---|
| `FACE_SERVICE_ENABLED` / `FACESWAP_ENABLED` | `false` | app/skills/image_generation_skill.py |
| `FACE_ENHANCE_ENABLED` | `true` | app/skills/image_generation_skill.py |
| `FACE_SERVICE_OMP_NUM_THREADS` / `FACESWAP_OMP_NUM_THREADS` | `4` | app/skills/face_enhance.py, app/skills/face_swap.py |
| `FACE_SERVICE_DET_SIZE` / `FACESWAP_DET_SIZE` | `640` | app/skills/face_swap.py |
| `FACE_SERVICE_MODEL_PATH` / `FACESWAP_MODEL_PATH` | `` | app/skills/face_swap.py |
| `FACE_ENHANCE_COLOR_CORRECTION` | `true` | app/skills/face_enhance.py |
| `FACE_ENHANCE_SHARPEN` | `true` | app/skills/face_enhance.py |
| `FACE_ENHANCE_SHARPEN_STRENGTH` | `0.5` | app/skills/face_enhance.py |
| `FACE_ENHANCE_BLEND` | `1.0` | app/skills/face_enhance.py |
| `FACE_ENHANCE_CODEFORMER_WEIGHT` | `0.7` | app/skills/face_enhance.py |
| `FACE_SERVICE_REQUEST_TIMEOUT` | `300` | app/skills/face_client.py |
| `COMFY_FACESWAP_WORKFLOW_FILE` | `` | app/skills/image_generation_skill.py |
| `COMFY_FACESWAP_BACKEND` | `` | app/skills/image_generation_skill.py |
| `COMFY_FACESWAP_VRAM_REQUIRED` | `0` | app/skills/image_generation_skill.py |
| `COMFY_MULTISWAP_WORKFLOW_FILE` | `` | app/skills/image_generation_skill.py |
| `COMFY_MULTISWAP_BACKEND` | `` | app/skills/image_generation_skill.py |
| `COMFY_MULTISWAP_UNET` | `` | app/skills/image_generation_skill.py |
| `COMFY_MULTISWAP_CLIP` | `` | app/skills/image_generation_skill.py |
| `DEFAULT_SWAP_MODE` | `comfyui` | app/skills/image_regenerate.py |

---

## Image Generation

| Variable | Default | Datei |
|---|---|---|
| `COMFY_IMAGEGEN_DEFAULT` | `` | app/skills/image_generation_skill.py |
| `OUTFIT_IMAGE_PROMPT_PREFIX` | `full body portrait` | app/routes/characters.py, app/core/expression_regen.py |
| `PROFILE_IMAGE_PROMPT_PREFIX` | `photorealistic, portrait, only head,` | app/routes/characters.py |
| `OUTFIT_IMAGEGEN_DEFAULT` | `` | app/routes/characters.py, app/core/expression_regen.py |
| `LOCATION_IMAGEGEN_DEFAULT` | `` | app/routes/world.py |
| `EXPRESSION_IMAGEGEN_DEFAULT` | `` | app/core/expression_regen.py |
| `IMAGE_ANALYSIS_PROMPT` | `` | app/skills/image_generation_skill.py |
| `IMAGE_ANALYSIS_LANGUAGE` | `de` | app/skills/image_generation_skill.py |
| `COMFY_FREE_MEMORY_BEFORE_RUN` | `true` | app/skills/image_backends.py |

---

## Instagram

| Variable | Default | Datei |
|---|---|---|
| `SKILL_INSTAGRAM_CAPTION_STYLE` | `casual` | app/skills/instagram_skill.py |
| `SKILL_INSTAGRAM_HASHTAG_COUNT` | `5` | app/skills/instagram_skill.py |
| `SKILL_INSTAGRAM_CAPTION_LANGUAGE` | `de` | app/skills/instagram_skill.py |
| `SKILL_INSTAGRAM_DEFAULT_POPULARITY` | `50` | app/skills/instagram_skill.py |
| `SKILL_INSTAGRAM_IMAGEGEN_DEFAULT` | `` | app/skills/instagram_skill.py |
| `SKILL_INSTAGRAM_POST_COOLDOWN_HOURS` | `12` | app/skills/instagram_skill.py |

---

## Skills

| Variable | Default | Datei |
|---|---|---|
| `SKILL_IMAGEGEN_{N}_ENABLED` | `true` | app/skills/image_generation_skill.py |
| `SKILL_IMAGEGEN_{N}_NAME` | `Instance_{N}` | app/skills/image_generation_skill.py |
| `SKILL_SEARX_NUM_RESULTS` | `10` | app/skills/searx_skill.py |
| `SKILL_OUTFIT_CREATION_LANGUAGE` | `en` | app/skills/outfit_creation_skill.py |
| `SKILL_OUTFIT_CREATION_MAX_DAILY_ITEMS` | `8` | app/skills/outfit_creation_skill.py |
| `SKILL_OUTFIT_CREATION_MAX_INVENTORY` | `60` | app/skills/outfit_creation_skill.py |
| `SKILL_DESCRIBEROOM_MAX_ROOMS` | `3` | app/skills/describe_room_skill.py |
| `SKILL_MARKDOWN_WRITER_FOLDERS` | `diary,notes,guides` | app/skills/markdown_writer_skill.py |
| `SKILL_MARKDOWN_WRITER_MAX_SIZE_KB` | `512` | app/skills/markdown_writer_skill.py |
| `SKILL_MARKDOWN_WRITER_MAX_FILES` | `50` | app/skills/markdown_writer_skill.py |
| `SKILL_MARKDOWN_WRITER_DEFAULT_FOLDER` | `diary` | app/skills/markdown_writer_skill.py |
| `SKILL_NOTIFY_USER_COOLDOWN_MIN` | `30` | app/skills/notify_user_skill.py |

---

## Proaktive Systeme

| Variable | Default | Datei |
|---|---|---|
| `PROACTIVE_MIN_IDLE_MINUTES` | `4` | app/core/proactive.py |
| `PROACTIVE_MIN_SCHEDULER_GAP_MINUTES` | `5` | app/core/proactive.py |

---

## Random Events

| Variable | Default | Datei |
|---|---|---|
| `EVENT_GENERATION_ENABLED` | `true` | app/core/random_events.py |
| `EVENT_BASE_PROBABILITY` | `0.10` | app/core/random_events.py |
| `EVENT_RESOLUTION_COOLDOWN_MINUTES` | `15` | app/core/random_events.py |
| `EVENT_RESOLUTION_PROACTIVE` | `true` | app/core/random_events.py |

---

## Story Engine

| Variable | Default | Datei |
|---|---|---|
| `STORY_ENGINE_ENABLED` | `true` | app/core/story_engine.py |
| `STORY_ENGINE_MAX_ACTIVE_ARCS` | `2` | app/core/story_engine.py |
| `STORY_ENGINE_COOLDOWN_HOURS` | `6` | app/core/story_engine.py |
| `STORY_ENGINE_MAX_BEATS` | `5` | app/core/story_engine.py |
| `STORY_ENGINE_BEAT_IMAGES` | `true` | app/core/story_engine.py |
| `STORY_ENGINE_IMAGEGEN_DEFAULT` | `` | app/core/story_engine.py |
| `STORY_ENGINE_BEAT_FACESWAP` | `false` | app/core/story_engine.py |

---

## Relationship System

| Variable | Default | Datei |
|---|---|---|
| `RELATIONSHIP_SUMMARY_ENABLED` | `true` | app/core/relationship_summary.py |
| `RELATIONSHIP_SUMMARY_INTERVAL_MINUTES` | `30` | app/core/relationship_summary.py |
| `RELATIONSHIP_SUMMARY_MAX_PER_RUN` | `5` | app/core/relationship_summary.py |
| `RELATIONSHIP_DECAY_STRENGTH` | `1` | app/core/relationship_decay.py |
| `RELATIONSHIP_DECAY_ROMANTIC` | `0.02` | app/core/relationship_decay.py |

---

## Group Chat

| Variable | Default | Datei |
|---|---|---|
| `GROUP_CHAT_THRESHOLD` | `2.0` | app/core/turn_taking.py |
| `GROUP_CHAT_MIN_RESPONDERS` | `1` | app/core/turn_taking.py |
| `GROUP_CHAT_MAX_RESPONDERS` | `3` | app/core/turn_taking.py |
| `GROUP_CHAT_MENTION_BOOST` | `5.0` | app/core/turn_taking.py |
| `GROUP_CHAT_COOLDOWN` | `2.0` | app/core/turn_taking.py |

---

## Social Reactions

| Variable | Default | Datei |
|---|---|---|
| `SOCIAL_REACTIONS_ENABLED` | `true` | app/core/social_reactions.py |

---

## Task Queue

| Variable | Default | Datei |
|---|---|---|
| `TASK_QUEUE_MAX_RETRIES` | `0` | app/core/task_queue.py |

---

## Logging

| Variable | Default | Datei |
|---|---|---|
| `LOG_LEVEL` | `INFO` | app/core/log.py |

---

## Storage

| Variable | Default | Datei |
|---|---|---|
| `STORAGE_DIR` | `./worlds/demo` | app/core/paths.py |
| `DEFAULT_THEME` | `default` | app/models/account.py |

---

## Animation (Together.ai)

| Variable | Default | Datei |
|---|---|---|
| `TOGETHER_ANIMATE_ENABLED` | `false` | app/skills/animate.py |
| `TOGETHER_ANIMATE_LABEL` | `Together.ai Cloud` | app/skills/animate.py |
| `TOGETHER_ANIMATE_API_URL` | `https://api.together.xyz` | app/skills/animate.py |
| `TOGETHER_ANIMATE_MODEL` | `` | app/skills/animate.py |
| `TOGETHER_ANIMATE_WIDTH` | `768` | app/skills/animate.py |
| `TOGETHER_ANIMATE_HEIGHT` | `768` | app/skills/animate.py |
| `TOGETHER_ANIMATE_SECONDS` | `5` | app/skills/animate.py |
| `TOGETHER_ANIMATE_POLL_INTERVAL` | `5.0` | app/skills/animate.py |
| `TOGETHER_ANIMATE_MAX_WAIT` | `600` | app/skills/animate.py |