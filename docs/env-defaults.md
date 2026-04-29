# Environment Variables mit Hardcoded Fallbacks

> **Note: Line numbers may have shifted since this document was last updated.**

Alle Env-Variablen im Code, die einen nicht-leeren Default haben.
Wenn die Variable in der `.env` fehlt, wird der Fallback-Wert verwendet.

---

## Sicherheit

| Variable | Fallback | Datei |
|---|---|---|
| `JWT_SECRET` | `your-secret-key-change-in-production` | app/core/auth.py:8 |

## Service URLs & Ports

| Variable | Fallback | Datei |
|---|---|---|
| `FACE_SERVICE_URL` | `http://localhost:8005` | app/server.py:129, app/core/dependencies.py:259, app/skills/face_client.py:25 |
| `FACE_SERVICE_PORT` | `8005` | face_service/server.py:118 |
| `SKILL_SEARX_URL` | `http://localhost:8888` | app/skills/searx_skill.py:37 |
| `TELEGRAM_API_URL` | `https://api.telegram.org/bot` | app/models/telegram_channel.py:47 |
| `PORT` | `8000` | app/scheduler/scheduler_manager.py:307 |

## TTS (Text-to-Speech)

| Variable | Fallback | Datei |
|---|---|---|
| `TTS_BACKEND` | `xtts` | app/core/tts_service.py:314 |
| `TTS_XTTS_URL` | `http://localhost:8020` | app/core/tts_service.py:318 |
| `TTS_XTTS_LANGUAGE` | `de` | app/core/tts_service.py:320 |
| `TTS_MAGPIE_URL` | `http://localhost:9000` | app/core/tts_service.py:323 |
| `TTS_MAGPIE_LANGUAGE` | `de-DE` | app/core/tts_service.py:325 |
| `TTS_F5_URL` | `http://localhost:7860` | app/core/tts_service.py:328 |
| `TTS_F5_SPEED` | `1.0` | app/core/tts_service.py:335 |
| `TTS_F5_NFE_STEPS` | `32` | app/core/tts_service.py:336 |

## LLM & Chat

| Variable | Fallback | Datei |
|---|---|---|
| `LLM_REQUEST_TIMEOUT` | `120` | app/core/llm_service.py:97, app/skills/image_generation_skill.py:743, app/skills/instagram_skill.py:266, app/routes/instagram.py:150 |
| `CHAT_HISTORY_WINDOW` | `20` | app/routes/chat.py:786 |
| `SYSTEM_PROMPT_CACHE_TIMEOUT` | `300` | app/routes/chat.py:799 |

## Knowledge

| Variable | Fallback | Datei |
|---|---|---|
| `KNOWLEDGE_MAX_ENTRIES` | `50` | app/models/knowledge.py:29 |
| `KNOWLEDGE_MAX_PROMPT_ENTRIES` | `20` | app/models/knowledge.py:34 |

## Face Enhancement / Swap

| Variable | Fallback | Datei |
|---|---|---|
| `FACESWAP_OMP_NUM_THREADS` | `4` | app/skills/face_enhance.py:73, app/skills/face_swap.py:106 |
| `FACESWAP_DET_SIZE` | `640` | app/skills/face_swap.py:116, :221 |
| `FACE_ENHANCE_SHARPEN_STRENGTH` | `0.5` | app/skills/face_enhance.py:100, :304 |
| `FACE_ENHANCE_BLEND` | `1.0` | app/skills/face_enhance.py:101, :297 |
| `FACE_ENHANCE_CODEFORMER_WEIGHT` | `0.7` | app/skills/face_enhance.py:104, :174 |

## Instagram

| Variable | Fallback | Datei |
|---|---|---|
| `SKILL_INSTAGRAM_CAPTION_STYLE` | `casual` | app/skills/instagram_skill.py:53 |
| `SKILL_INSTAGRAM_HASHTAG_COUNT` | `5` | app/skills/instagram_skill.py:54 |
| `SKILL_INSTAGRAM_CAPTION_LANGUAGE` | `de` | app/skills/instagram_skill.py:55 |
| `SKILL_INSTAGRAM_DEFAULT_POPULARITY` | `50` | app/skills/instagram_skill.py:56 |

## Proaktive Benachrichtigungen

| Variable | Fallback | Datei |
|---|---|---|
| `PROACTIVE_MIN_IDLE_MINUTES` | `4` | app/core/proactive.py |
| `PROACTIVE_MIN_SCHEDULER_GAP_MINUTES` | `5` | app/core/proactive.py |

## Sonstiges

| Variable | Fallback | Datei |
|---|---|---|
| `OUTFIT_IMAGE_PROMPT_PREFIX` | `full body portrait` | app/routes/characters.py:611 |
| `SKILL_SEARX_NUM_RESULTS` | `10` | app/skills/searx_skill.py:42 |
| `DAILY_SUMMARY_DAYS` | `7` | app/utils/history_manager.py:227, :422 |
| `DEFAULT_THEME` | `default` | app/models/user.py:321 |
| `SKILL_IMAGEGEN_NAME` | `ImageGenerator` | app/skills/image_generation_skill.py:75 |
| `SKILL_INSTAGRAM_NAME` | `Instagram` | app/skills/instagram_skill.py:45 |
| `SKILL_TALKTO_NAME` | `TalkTo` | app/skills/talkto_skill.py:51 |
| `SKILL_SETLOCATION_NAME` | `SetLocation` | app/skills/set_location_skill.py:50 |
| `SKILL_NOTIFY_USER_NAME` | `SendNotification` | app/skills/notify_user_skill.py |
| `SKILL_NOTIFY_USER_DESCRIPTION` | `Send a notification message to the user...` | app/skills/notify_user_skill.py |
